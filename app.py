"""Flask API for video upload, feed delivery, and recommendation analytics."""

import json
import hashlib
import hmac
import logging
import math
import os
import re
import time
import uuid
from functools import wraps
from urllib.parse import quote, urlencode

import requests
import firebase_admin
from google.cloud.firestore_v1.base_query import FieldFilter
from google.api_core.exceptions import FailedPrecondition
from firebase_admin import auth as firebase_auth, credentials, firestore, messaging
from flask import Flask, Response, g, jsonify, render_template, request, stream_with_context
from werkzeug.utils import secure_filename

from algorithm import rank_videos
from analytics import create_watch, finish_watch, record_event


app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "/tmp/uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY")
IDENTITY_TOOLKIT_URL = "https://identitytoolkit.googleapis.com/v1"
SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
PLAYBACK_SIGNING_SECRET = os.getenv("PLAYBACK_SIGNING_SECRET") or BOT_TOKEN
PLAYBACK_URL_TTL_SECONDS = 15 * 60
BACKEND_BASE_URL = os.getenv(
    "BACKEND_BASE_URL",
    "https://my-cam-tik-server-mhbn.vercel.app",
).rstrip("/")


def _connect_firebase():
    firebase_data = os.getenv("FIREBASE_CREDENTIALS")
    try:
        if firebase_data:
            credential = credentials.Certificate(json.loads(firebase_data))
        else:
            credential_path = os.path.join(os.path.dirname(__file__), "firebase_credentials.json")
            credential = credentials.Certificate(credential_path)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credential)
        return firestore.client()
    except Exception:
        logger.exception("Firebase initialization failed")
        return None


db = _connect_firebase()


def _error(message, status=400):
    return jsonify({"error": message}), status


def _require_auth(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return _error("Authentication required", 401)
        try:
            claims = firebase_auth.verify_id_token(token.strip(), check_revoked=True)
        except Exception:
            return _error("Invalid or expired session", 401)
        user_id = claims.get("uid")
        if not user_id:
            return _error("Invalid session", 401)
        g.auth_user_id = user_id
        g.auth_email = claims.get("email")
        return handler(*args, **kwargs)

    return wrapped


def _optional_authenticated_user_id():
    authorization = request.headers.get("Authorization", "")
    if not authorization:
        return None, None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None, _error("Invalid authorization header", 401)
    try:
        claims = firebase_auth.verify_id_token(token.strip(), check_revoked=True)
        user_id = claims.get("uid")
        return (user_id, None) if user_id else (None, _error("Invalid session", 401))
    except Exception:
        return None, _error("Invalid or expired session", 401)


def _identity_toolkit_request(endpoint, payload):
    if not FIREBASE_WEB_API_KEY:
        return None, _error("Firebase Authentication is not configured", 503)
    try:
        response = requests.post(
            f"{IDENTITY_TOOLKIT_URL}/{endpoint}",
            params={"key": FIREBASE_WEB_API_KEY},
            json=payload,
            timeout=(5, 15),
        )
    except requests.RequestException:
        logger.warning("Firebase Authentication request failed")
        return None, _error("Authentication service is unavailable", 503)

    try:
        result = response.json()
    except ValueError:
        result = {}
    if response.ok:
        return result, None

    error_code = str((result.get("error") or {}).get("message", ""))
    if error_code == "EMAIL_EXISTS":
        return None, _error("An account with this email already exists", 409)
    if error_code in {"INVALID_PASSWORD", "EMAIL_NOT_FOUND", "INVALID_LOGIN_CREDENTIALS"}:
        return None, _error("Email or password is incorrect", 401)
    if error_code in {"INVALID_EMAIL", "WEAK_PASSWORD"}:
        return None, _error("Email or password does not meet requirements", 400)
    if error_code == "TOO_MANY_ATTEMPTS_TRY_LATER":
        return None, _error("Too many attempts. Try again later", 429)
    logger.warning("Firebase Authentication rejected a request: %s", error_code or response.status_code)
    return None, _error("Authentication request was rejected", 400)


def _account_profile(user_id, email=None):
    snapshot = db.collection("users").document(user_id).get()
    profile = snapshot.to_dict() or {} if snapshot.exists else {}
    followed = profile.get("followed_creators") or []
    if isinstance(followed, dict):
        following_count = len(followed)
    elif isinstance(followed, (list, tuple, set)):
        following_count = len(followed)
    else:
        following_count = None
    return {
        "user_id": user_id,
        "username": profile.get("username") or "",
        "email": profile.get("email") or email or "",
        "profile_image": profile.get("profile_image"),
        "bio": profile.get("bio") or "",
        "created_at": profile.get("created_at").isoformat()
        if hasattr(profile.get("created_at"), "isoformat") else None,
        "followers_count": _query_count(
            db.collection("users").where(
                filter=FieldFilter("followed_creators", "array_contains", user_id),
            ),
        ),
        "following_count": following_count,
        "videos_count": _query_count(
            db.collection("videos").where(
                filter=FieldFilter("creator_id", "==", user_id),
            ),
        ),
        "verified_status": profile.get("verified_status"),
    }


def _username_value(value):
    if not isinstance(value, str):
        return None
    username = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.]{3,24}", username):
        return None
    return username


def _identity_request(endpoint, payload):
    if not FIREBASE_WEB_API_KEY:
        return None, _error("Firebase Authentication is not configured", 503)
    try:
        response = requests.post(
            f"{IDENTITY_TOOLKIT_URL}/{endpoint}",
            params={"key": FIREBASE_WEB_API_KEY},
            json=payload,
            timeout=(5, 15),
        )
    except requests.RequestException:
        logger.warning("Firebase Authentication request failed")
        return None, _error("Authentication service is unavailable", 503)
    try:
        result = response.json()
    except ValueError:
        result = {}
    if response.ok:
        return result, None

    error_code = str((result.get("error") or {}).get("message", ""))
    if error_code == "EMAIL_EXISTS":
        return None, _error("An account with this email already exists", 409)
    if error_code in {"INVALID_PASSWORD", "EMAIL_NOT_FOUND", "INVALID_LOGIN_CREDENTIALS"}:
        return None, _error("Email or password is incorrect", 401)
    if error_code in {"INVALID_EMAIL", "WEAK_PASSWORD"}:
        return None, _error("Email or password does not meet requirements", 400)
    if error_code == "TOO_MANY_ATTEMPTS_TRY_LATER":
        return None, _error("Too many attempts. Try again later", 429)
    logger.warning("Firebase Authentication rejected a request: %s", error_code or response.status_code)
    return None, _error("Authentication request was rejected", 400)


def _create_profile_for_identity(user_id, email, username):
    if db is None:
        return False
    username_ref = db.collection("usernames").document(username)
    user_ref = db.collection("users").document(user_id)
    transaction = db.transaction()

    @firestore.transactional
    def _reserve(transaction):
        username_snapshot = username_ref.get(transaction=transaction)
        if username_snapshot.exists:
            return False
        transaction.create(username_ref, {"user_id": user_id})
        transaction.set(user_ref, {
            "user_id": user_id,
            "username": username,
            "email": email,
            "profile_image": None,
            "bio": "",
            "created_at": firestore.SERVER_TIMESTAMP,
            "verified_status": False,
            "followed_creators": [],
        }, merge=True)
        return True

    return _reserve(transaction)


def _safe_trim(value, limit=200):
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit] if len(text) > limit else text


def _notification_doc_key(recipient_id, actor_id, notification_type, target_id=None):
    parts = [recipient_id or "", actor_id or "", notification_type or "", target_id or ""]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _user_profile_snapshot(user_id):
    if not user_id or db is None:
        return {}
    snapshot = db.collection("users").document(user_id).get()
    return snapshot.to_dict() or {} if snapshot.exists else {}


def _create_notification(recipient_id, actor_id, notification_type, title, body, video_id=None, deep_link=None, actor_username=None, actor_image=None, dedupe_key=None):
    if db is None or not recipient_id or not actor_id or not notification_type:
        return None
    if recipient_id == actor_id:
        return None
    dedupe_key = dedupe_key or _notification_doc_key(recipient_id, actor_id, notification_type, video_id)
    existing_ref = db.collection("notifications").document(dedupe_key)
    if existing_ref.get().exists:
        return existing_ref.id
    notification = {
        "recipient_id": recipient_id,
        "actor_id": actor_id,
        "type": notification_type,
        "title": _safe_trim(title, 120),
        "body": _safe_trim(body, 240),
        "video_id": video_id or None,
        "actor_username": _safe_trim(actor_username, 80),
        "actor_image": _safe_trim(actor_image, 500),
        "read": False,
        "created_at": firestore.SERVER_TIMESTAMP,
        "deep_link": deep_link or None,
    }
    existing_ref.set(notification)
    return existing_ref.id


def _send_fcm_to_user(recipient_id, title, body, data=None, notification_type="general"):
    if db is None or not recipient_id:
        return 0
    tokens = []
    try:
        devices = db.collection("user_devices").where(filter=FieldFilter("user_id", "==", recipient_id)).stream()
        for doc in devices:
            token = (doc.to_dict() or {}).get("token")
            if token:
                tokens.append(token)
    except Exception:
        logger.exception("Could not load FCM tokens for user %s", recipient_id)
        return 0
    if not tokens:
        return 0
    payload = {
        "notification": {
            "title": _safe_trim(title, 120),
            "body": _safe_trim(body, 240),
        },
        "data": {**(data or {}), "type": notification_type, "recipient_id": recipient_id},
        "android": {
            "priority": "high",
            "notification": {
                "channel_id": "camtik_notifications",
                "default_sound": True,
                "default_vibrate_timings": True,
            },
        },
        "apns": {"payload": {"aps": {"sound": "default"}}},
    }
    try:
        result = messaging.send_each_for_multicast(messaging.MulticastMessage(**payload), dry_run=False)
    except Exception:
        logger.exception("FCM send failed for user %s", recipient_id)
        return 0
    invalid_tokens = []
    for index, response in enumerate(result.responses):
        if not response.success:
            error_code = response.exception.code if response.exception else None
            if error_code in {messaging.UnregisteredError.code, "INVALID_ARGUMENT", "NOT_FOUND"}:
                invalid_tokens.append(tokens[index])
    for token in invalid_tokens:
        try:
            doc = db.collection("user_devices").document(hashlib.sha256(token.encode("utf-8")).hexdigest())
            if doc.get().exists:
                doc.delete()
        except Exception:
            logger.warning("Could not remove invalid FCM token for user %s", recipient_id)
    return result.success_count


def _notification_deep_link(notification_type, video_id=None, actor_id=None):
    if notification_type == "follow" and actor_id:
        return f"profile:{actor_id}"
    if video_id:
        return f"video:{video_id}"
    if actor_id:
        return f"profile:{actor_id}"
    return "inbox:notifications"


def _ensure_identity_profile(user_id, email, preferred_username=None):
    profile_ref = db.collection("users").document(user_id)
    snapshot = profile_ref.get()
    if snapshot.exists:
        return True
    base = _username_value(preferred_username) or _username_value((email or "").split("@", 1)[0])
    if not base:
        base = f"user_{user_id[:8]}"
    candidate = f"{base[:17]}_{user_id[:6]}"
    return _create_profile_for_identity(user_id, email or "", candidate)


def _auth_session_response(identity_result, preferred_username=None):
    user_id = identity_result.get("localId")
    id_token = identity_result.get("idToken")
    refresh_token = identity_result.get("refreshToken")
    if not user_id or not id_token or not refresh_token:
        return _error("Authentication service returned an incomplete session", 502)
    try:
        claims = firebase_auth.verify_id_token(id_token)
    except Exception:
        return _error("Authentication service returned an invalid session", 502)
    if claims.get("uid") != user_id:
        return _error("Authentication service returned an invalid session", 502)
    if not _ensure_identity_profile(user_id, identity_result.get("email", ""), preferred_username):
        return _error("Could not initialize user profile", 500)
    return jsonify({
        "access_token": id_token,
        "refresh_token": refresh_token,
        "expires_in": int(identity_result.get("expiresIn", 3600)),
        "user": _account_profile(user_id, identity_result.get("email")),
    })


def _json_body():
    return request.get_json(silent=True) or {}


def _playback_signature(video_id, expires):
    secret = PLAYBACK_SIGNING_SECRET
    if not secret:
        return None
    message = f"{video_id}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _normalize_video_numbers(video):
    integer_fields = (
        "views", "likes", "comments", "shares", "watch_sessions",
        "rewatches", "completed_views", "skipped_views",
    )
    float_fields = (
        "watch_time", "duration_seconds", "completion_total", "completion_rate",
        "skip_rate", "score", "trending_score",
    )
    for field in integer_fields:
        try:
            number = float(video.get(field, 0) or 0)
            video[field] = int(number) if math.isfinite(number) else 0
        except (TypeError, ValueError, OverflowError):
            video[field] = 0
    for field in float_fields:
        try:
            number = float(video.get(field, 0.0) or 0.0)
            video[field] = number if math.isfinite(number) else 0.0
        except (TypeError, ValueError, OverflowError):
            video[field] = 0.0
    return video


def _event_response(event_type, user_id, video_id, payload=None, event_id=None):
    if db is None:
        return _error("Firebase not connected", 503)
    user_id = str(user_id).strip() if user_id is not None else None
    video_id = str(video_id).strip() if video_id is not None else None
    try:
        saved = record_event(db, event_type, user_id, video_id, payload, event_id)
        return jsonify({"success": True, "duplicate": not saved})
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Failed to record %s event", event_type)
        return _error("Unable to record event", 500)


def _profile_for(user_id):
    if not user_id:
        return {}, []
    snapshot = db.collection("users").document(user_id).get()
    profile = snapshot.to_dict() or {} if snapshot.exists else {}
    followed = profile.get("followed_creators") or []
    if isinstance(followed, dict):
        followed = list(followed.keys())
    return profile.get("interests") or {}, followed


def _query_count(query):
    result = query.count().get()
    return int(result[0][0].value) if result else 0


def _recommendation_page(user_id, limit, cursor=None):
    limit = min(100, max(1, int(limit)))
    query = (
        db.collection("videos")
        .order_by("trending_score", direction=firestore.Query.DESCENDING)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    if cursor:
        cursor_snapshot = db.collection("videos").document(cursor).get()
        if cursor_snapshot.exists:
            query = query.start_after(cursor_snapshot)

    snapshots = []
    try:
        snapshots = list(query.stream())
    except FailedPrecondition:
        logger.warning("Recommendation index missing; using newest-first fallback")
    if not snapshots:
        fallback_query = db.collection("videos").order_by(
            "created_at", direction=firestore.Query.DESCENDING
        )
        if cursor:
            cursor_snapshot = db.collection("videos").document(cursor).get()
            if cursor_snapshot.exists:
                fallback_query = fallback_query.start_after(cursor_snapshot)
        snapshots = list(fallback_query.limit(limit).stream())

    candidates = []
    for snapshot in snapshots:
        item = _normalize_video_numbers(snapshot.to_dict() or {})
        item["video_id"] = snapshot.id
        candidates.append(item)

    interests, followed = _profile_for(user_id)
    seen = set()
    if user_id:
        recent = (
            db.collection("watch_history")
            .where("user_id", "==", user_id)
            .order_by("start_time", direction=firestore.Query.DESCENDING)
            .limit(100)
        )
        seen = {doc.to_dict().get("video_id") for doc in recent.stream()}

    ranked = rank_videos(candidates, interests, followed, seen)
    next_cursor = snapshots[-1].id if len(snapshots) == limit else None
    return ranked, next_cursor


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/auth/signup", methods=["POST"])
def auth_signup():
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password")
    username = _username_value(data.get("username"))
    if not email or len(email) > 320 or not isinstance(password, str) or len(password) < 8:
        return _error("A valid email and password of at least 8 characters are required")
    if not username:
        return _error("Username must be 3-24 characters: letters, numbers, dot, or underscore")

    result, error = _identity_request("accounts:signUp", {
        "email": email,
        "password": password,
        "displayName": username,
        "returnSecureToken": True,
    })
    if error:
        return error

    user_id = result.get("localId")
    if not user_id:
        return _error("Authentication service returned an incomplete account", 502)
    try:
        if not _create_profile_for_identity(user_id, email, username):
            firebase_auth.delete_user(user_id)
            return _error("Username is already taken", 409)
        return _auth_session_response(result, username)
    except Exception:
        logger.exception("Could not initialize a newly registered account")
        try:
            firebase_auth.delete_user(user_id)
        except Exception:
            logger.warning("Could not clean up a partially initialized auth account")
        return _error("Could not initialize user profile", 500)


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = _json_body()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password")
    if not email or not isinstance(password, str) or not password:
        return _error("Email and password are required")
    result, error = _identity_request("accounts:signInWithPassword", {
        "email": email,
        "password": password,
        "returnSecureToken": True,
    })
    if error:
        return error
    return _auth_session_response(result)


@app.route("/auth/refresh", methods=["POST"])
def auth_refresh():
    if not FIREBASE_WEB_API_KEY:
        return _error("Firebase Authentication is not configured", 503)
    refresh_token = str(_json_body().get("refresh_token", "")).strip()
    if not refresh_token:
        return _error("refresh_token is required")
    try:
        response = requests.post(
            SECURE_TOKEN_URL,
            params={"key": FIREBASE_WEB_API_KEY},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=(5, 15),
        )
    except requests.RequestException:
        logger.warning("Firebase token refresh request failed")
        return _error("Authentication service is unavailable", 503)
    try:
        token_data = response.json()
    except ValueError:
        token_data = {}
    if not response.ok:
        return _error("Session expired. Please log in again", 401)
    identity_result = {
        "localId": token_data.get("user_id"),
        "idToken": token_data.get("id_token"),
        "refreshToken": token_data.get("refresh_token"),
        "expiresIn": token_data.get("expires_in", 3600),
        "email": token_data.get("email", ""),
    }
    return _auth_session_response(identity_result)


@app.route("/auth/logout", methods=["POST"])
@_require_auth
def auth_logout():
    try:
        firebase_auth.revoke_refresh_tokens(g.auth_user_id)
        return jsonify({"success": True})
    except Exception:
        logger.exception("Could not revoke user session")
        return _error("Unable to log out", 500)


@app.route("/me", methods=["GET"])
@_require_auth
def get_current_user():
    if db is None:
        return _error("Firebase not connected", 503)
    return jsonify(_account_profile(g.auth_user_id, g.auth_email))


@app.route("/profile", methods=["PUT"])
@_require_auth
def update_current_profile():
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    user_ref = db.collection("users").document(g.auth_user_id)
    current_snapshot = user_ref.get()
    if not current_snapshot.exists:
        return _error("User profile not found", 404)
    current = current_snapshot.to_dict() or {}
    updates = {}

    if "username" in data:
        username = _username_value(data.get("username"))
        if not username:
            return _error("Username must be 3-24 characters: letters, numbers, dot, or underscore")
        updates["username"] = username
    if "bio" in data:
        bio = data.get("bio")
        if not isinstance(bio, str) or len(bio) > 160:
            return _error("Bio must be text of 160 characters or fewer")
        updates["bio"] = bio.strip()
    if "profile_image" in data:
        image_url = data.get("profile_image")
        if image_url is not None and (
            not isinstance(image_url, str)
            or len(image_url) > 2048
            or not image_url.startswith("https://")
        ):
            return _error("profile_image must be an HTTPS URL or null")
        updates["profile_image"] = image_url
    if not updates:
        return _error("No profile fields to update")

    old_username = _username_value(current.get("username"))
    new_username = updates.get("username", old_username)
    transaction = db.transaction()

    @firestore.transactional
    def _save_profile(transaction):
        username_ref = db.collection("usernames").document(new_username) if new_username else None
        username_snapshot = username_ref.get(transaction=transaction) if username_ref else None
        if (
            new_username
            and new_username != old_username
            and username_snapshot.exists
            and (username_snapshot.to_dict() or {}).get("user_id") != g.auth_user_id
        ):
            return False
        if new_username and new_username != old_username:
            transaction.set(username_ref, {"user_id": g.auth_user_id})
        if old_username and old_username != new_username:
            transaction.delete(db.collection("usernames").document(old_username))
        transaction.set(user_ref, updates, merge=True)
        return True

    try:
        if not _save_profile(transaction):
            return _error("Username is already taken", 409)
        return jsonify(_account_profile(g.auth_user_id, g.auth_email))
    except Exception:
        logger.exception("Profile update failed")
        return _error("Unable to update profile", 500)


@app.route("/notifications/device", methods=["POST"])
@_require_auth
def register_device_token():
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    token = str(data.get("token", "")).strip()
    platform = str(data.get("platform", "android")).strip().lower() or "android"
    if not token or len(token) < 32 or len(token) > 2048:
        return _error("A valid device token is required")
    if platform not in {"android", "ios", "web"}:
        platform = "android"
    now = firestore.SERVER_TIMESTAMP
    doc_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
    doc_ref = db.collection("user_devices").document(doc_id)
    doc_ref.set({
        "user_id": g.auth_user_id,
        "token": token,
        "platform": platform,
        "created_at": now,
        "updated_at": now,
        "last_seen": now,
    }, merge=True)
    if not doc_ref.get().exists:
        doc_ref.set({
            "user_id": g.auth_user_id,
            "token": token,
            "platform": platform,
            "created_at": now,
            "updated_at": now,
            "last_seen": now,
        })
    return jsonify({"success": True})


@app.route("/notifications", methods=["GET"])
@_require_auth
def list_notifications():
    if db is None:
        return _error("Firebase not connected", 503)
    try:
        limit = min(100, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        return _error("limit must be an integer")
    docs = (
        db.collection("notifications")
        .where(filter=FieldFilter("recipient_id", "==", g.auth_user_id))
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    notifications = []
    for document in docs:
        item = document.to_dict() or {}
        notifications.append({
            "id": document.id,
            "type": item.get("type"),
            "title": item.get("title"),
            "body": item.get("body"),
            "actor_id": item.get("actor_id"),
            "actor_username": item.get("actor_username"),
            "actor_image": item.get("actor_image"),
            "video_id": item.get("video_id"),
            "read": bool(item.get("read", False)),
            "created_at": item.get("created_at"),
            "deep_link": item.get("deep_link"),
        })
    return jsonify(notifications)


@app.route("/notifications/unread-count", methods=["GET"])
@_require_auth
def unread_notification_count():
    if db is None:
        return _error("Firebase not connected", 503)
    count = _query_count(
        db.collection("notifications")
        .where(filter=FieldFilter("recipient_id", "==", g.auth_user_id))
        .where(filter=FieldFilter("read", "==", False))
    )
    return jsonify({"count": count})


@app.route("/notifications/<notification_id>/read", methods=["POST"])
@_require_auth
def mark_notification_read(notification_id):
    if db is None:
        return _error("Firebase not connected", 503)
    doc_ref = db.collection("notifications").document(notification_id)
    snapshot = doc_ref.get()
    if not snapshot.exists:
        return _error("Notification not found", 404)
    notification = snapshot.to_dict() or {}
    if notification.get("recipient_id") != g.auth_user_id:
        return _error("Forbidden", 403)
    doc_ref.update({"read": True})
    return jsonify({"success": True, "read": True})


@app.route("/notifications/read-all", methods=["POST"])
@_require_auth
def mark_all_notifications_read():
    if db is None:
        return _error("Firebase not connected", 503)
    docs = (
        db.collection("notifications")
        .where(filter=FieldFilter("recipient_id", "==", g.auth_user_id))
        .where(filter=FieldFilter("read", "==", False))
        .stream()
    )
    for document in docs:
        document.reference.update({"read": True})
    return jsonify({"success": True})


@app.route("/upload", methods=["POST"])
@_require_auth
def upload():
    if "video" not in request.files:
        return _error("No video found")
    if not BOT_TOKEN or not CHANNEL_ID:
        return _error("Video storage is not configured", 503)
    if db is None:
        return _error("Firebase not connected", 503)

    video = request.files["video"]
    filename = secure_filename(video.filename or "")
    if not filename:
        return _error("A valid filename is required")
    title = (request.form.get("title") or "No title").strip()[:300]
    try:
        duration = max(0.0, float(request.form.get("duration_seconds", 0) or 0))
    except ValueError:
        return _error("duration_seconds must be a number")
    tags = [tag.strip().lower()[:64] for tag in request.form.get("tags", "").split(",") if tag.strip()]
    category = request.form.get("category", "").strip().lower()[:64]
    creator_id = g.auth_user_id
    temp_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4().hex}_{filename}")

    try:
        video.save(temp_path)
        telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
        with open(temp_path, "rb") as video_file:
            response = requests.post(
                telegram_url,
                data={"chat_id": CHANNEL_ID, "caption": title},
                files={"video": (filename, video_file)},
                timeout=120,
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            logger.error("Telegram upload rejected: %s", result.get("description", "unknown error"))
            return _error("Telegram video upload failed", 502)
        file_id = result["result"]["video"]["file_id"]
        doc_ref = db.collection("videos").document()
        doc_ref.set({
            "video_id": doc_ref.id,
            "title": title,
            "file_id": file_id,
            "filename": filename,
            "creator_id": creator_id,
            "tags": tags,
            "category": category,
            "duration_seconds": duration,
            "created_at": firestore.SERVER_TIMESTAMP,
            "views": 0,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "watch_time": 0.0,
            "watch_sessions": 0,
            "rewatches": 0,
            "completed_views": 0,
            "skipped_views": 0,
            "completion_total": 0.0,
            "completion_rate": 0.0,
            "skip_rate": 0.0,
            "score": 0.0,
            "trending_score": 0.12,
            "score_updated_at": firestore.SERVER_TIMESTAMP,
        })
        return jsonify({"success": True, "file_id": file_id, "video_id": doc_ref.id})
    except requests.RequestException:
        logger.exception("Telegram upload request failed")
        return _error("Telegram video upload failed", 502)
    except Exception:
        logger.exception("Video upload failed")
        return _error("Unable to upload video", 500)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.route("/feed", methods=["GET"])
def feed():
    if db is None:
        return _error("Firebase not connected", 503)
    try:
        user_id = request.args.get("user_id")
        items, next_cursor = _recommendation_page(
            user_id, request.args.get("limit", 50), request.args.get("cursor")
        )
        for item in items:
            item["creator_id"] = item.get("creator_id") or None
        response = jsonify(items)
        response.headers["X-Next-Cursor"] = next_cursor or ""
        return response
    except (TypeError, ValueError):
        return _error("limit must be an integer")
    except Exception:
        logger.exception("Feed request failed")
        return _error("Unable to load feed", 500)


@app.route("/user/<creator_id>", methods=["GET"])
def get_user_profile(creator_id):
    if db is None:
        return _error("Firebase not connected", 503)
    creator_id = creator_id.strip()
    if not creator_id:
        return _error("creator_id is required")
    try:
        snapshot = db.collection("users").document(creator_id).get()
        profile_exists = snapshot.exists
        profile = snapshot.to_dict() or {} if profile_exists else {}

        videos_count = _query_count(
            db.collection("videos").where(
                filter=FieldFilter("creator_id", "==", creator_id),
            ),
        )
        followers_count = _query_count(
            db.collection("users").where(
                filter=FieldFilter("followed_creators", "array_contains", creator_id),
            ),
        )
        if not profile_exists and videos_count == 0:
            return _error("Creator not found", 404)

        followed_creators = profile.get("followed_creators") or []
        if isinstance(followed_creators, dict):
            following_count = len(followed_creators)
        elif isinstance(followed_creators, (list, tuple, set)):
            following_count = len(followed_creators)
        else:
            following_count = None

        return jsonify({
            "username": str(profile.get("username") or creator_id),
            "profile_image": profile.get("profile_image") or None,
            "bio": str(profile.get("bio") or ""),
            "followers_count": followers_count,
            "following_count": following_count,
            "videos_count": videos_count,
            "verified_status": profile.get("verified_status"),
        })
    except Exception:
        logger.exception("Creator profile request failed")
        return _error("Unable to load creator profile", 500)


@app.route("/playback/<video_id>", methods=["GET"])
def playback_url(video_id):
    if db is None:
        return _error("Firebase not connected", 503)
    if not BOT_TOKEN or not PLAYBACK_SIGNING_SECRET:
        return _error("Video playback is not configured", 503)
    try:
        snapshot = db.collection("videos").document(video_id).get()
        if not snapshot.exists:
            return _error("Video not found", 404)
        video = snapshot.to_dict() or {}
        if not video.get("file_id"):
            return _error("Video file is unavailable", 404)

        expires = int(time.time()) + PLAYBACK_URL_TTL_SECONDS
        signature = _playback_signature(video_id, expires)
        query = urlencode({"expires": expires, "sig": signature})
        stream_url = (
            f"{BACKEND_BASE_URL}/stream/{quote(video_id, safe='')}?{query}"
        )
        return jsonify({
            "url": stream_url,
            "expires_at": expires,
        })
    except Exception:
        logger.exception("Could not create playback URL")
        return _error("Unable to create playback URL", 500)


@app.route("/stream/<video_id>", methods=["GET", "HEAD"])
def stream_video(video_id):
    if db is None or not BOT_TOKEN or not PLAYBACK_SIGNING_SECRET:
        return _error("Video playback is not configured", 503)

    expires_value = request.args.get("expires", "")
    signature = request.args.get("sig", "")
    try:
        expires = int(expires_value)
    except (TypeError, ValueError):
        return _error("Invalid playback URL", 403)

    now = int(time.time())
    if expires < now or expires > now + PLAYBACK_URL_TTL_SECONDS + 5:
        return _error("Playback URL expired", 403)
    expected_signature = _playback_signature(video_id, expires)
    if not expected_signature or not hmac.compare_digest(signature, expected_signature):
        return _error("Invalid playback URL", 403)

    try:
        snapshot = db.collection("videos").document(video_id).get()
        if not snapshot.exists:
            return _error("Video not found", 404)
        file_id = (snapshot.to_dict() or {}).get("file_id")
        if not file_id:
            return _error("Video file is unavailable", 404)

        telegram_response = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=(5, 20),
        )
        telegram_response.raise_for_status()
        telegram_result = telegram_response.json()
        file_path = (telegram_result.get("result") or {}).get("file_path")
        if not telegram_result.get("ok") or not file_path:
            return _error("Video file is unavailable", 502)

        upstream_headers = {"Accept-Encoding": "identity"}
        for header in ("Range", "If-Range"):
            if request.headers.get(header):
                upstream_headers[header] = request.headers[header]
        upstream = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
            headers=upstream_headers,
            stream=True,
            timeout=(5, 60),
        )
    except requests.RequestException as exc:
        logger.warning("Telegram playback request failed: %s", type(exc).__name__)
        return _error("Unable to retrieve video", 502)
    except Exception:
        logger.exception("Could not resolve video for streaming")
        return _error("Unable to retrieve video", 502)

    if upstream.status_code not in (200, 206, 416):
        upstream.close()
        return _error("Video source rejected the request", 502)

    response_headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    for header in ("Content-Length", "Content-Range"):
        if upstream.headers.get(header):
            response_headers[header] = upstream.headers[header]

    if request.method == "HEAD" or upstream.status_code == 416:
        upstream.close()
        return Response(status=upstream.status_code, headers=response_headers)

    def generate_video():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = Response(
        stream_with_context(generate_video()),
        status=upstream.status_code,
        headers=response_headers,
    )
    response.call_on_close(upstream.close)
    return response


@app.route("/recommendations", methods=["GET"])
def recommendations():
    if db is None:
        return _error("Firebase not connected", 503)
    try:
        user_id = request.args.get("user_id")
        items, next_cursor = _recommendation_page(user_id, request.args.get("limit", 20), request.args.get("cursor"))
        return jsonify({"items": items, "next_cursor": next_cursor})
    except (TypeError, ValueError):
        return _error("limit must be an integer")
    except Exception:
        logger.exception("Recommendation request failed")
        return _error("Unable to load recommendations", 500)


@app.route("/watch/start", methods=["POST"])
@_require_auth
def start_watch():
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    user_id = g.auth_user_id
    video_id = str(data.get("video_id", "")).strip()
    if not video_id:
        return _error("video_id is required")
    try:
        watch_id = create_watch(db, user_id or None, video_id)
        return jsonify({"success": True, "watch_id": watch_id})
    except Exception:
        logger.exception("Could not start watch session")
        return _error("Unable to start watch session", 500)


@app.route("/watch/finish", methods=["POST"])
@_require_auth
def finish_watch_route():
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    watch_id = str(data.get("watch_id", "")).strip()
    if not watch_id:
        return _error("watch_id is required")
    try:
        result = finish_watch(db, watch_id, data.get("seconds", 0), data.get("duration_seconds", 0), g.auth_user_id)
        return jsonify({"success": True, **result})
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Could not finish watch session")
        return _error("Unable to finish watch session", 500)


@app.route("/events", methods=["POST"])
@_require_auth
def events():
    data = _json_body()
    event_type = str(data.get("type", "")).strip().lower()
    user_id = g.auth_user_id
    video_id = str(data.get("video_id", "")).strip() or None
    if event_type not in {"view", "watch", "skip", "like", "comment", "share", "follow"}:
        return _error("Unsupported event type")
    if event_type != "follow" and not video_id:
        return _error("video_id is required for this event")
    if event_type == "follow" and (not user_id or not data.get("creator_id")):
        return _error("user_id and creator_id are required for follow events")
    payload = {key: data[key] for key in ("seconds", "duration_seconds", "creator_id") if key in data}
    return _event_response(event_type, user_id, video_id, payload, data.get("event_id"))


@app.route("/comments/<video_id>", methods=["GET"])
def list_comments(video_id):
    if db is None:
        return _error("Firebase not connected", 503)
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
        video_ref = db.collection("videos").document(video_id)
        if not video_ref.get().exists:
            return _error("Video not found", 404)
        documents = (
            video_ref.collection("comments")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        comments = []
        for document in documents:
            item = document.to_dict() or {}
            created_at = item.get("created_at")
            comments.append({
                "comment_id": document.id,
                "user_id": item.get("user_id") or "",
                "username": item.get("username") or "",
                "text": item.get("text") or "",
                "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else None,
            })
        comments.reverse()
        return jsonify(comments)
    except (TypeError, ValueError):
        return _error("limit must be an integer")
    except Exception:
        logger.exception("Comment listing failed")
        return _error("Unable to load comments", 500)


@app.route("/comment/<video_id>", methods=["POST"])
@_require_auth
def add_comment(video_id):
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    user_id = g.auth_user_id
    text = str(data.get("text", "")).strip()
    if not text:
        return _error("text is required")
    if len(text) > 2000:
        return _error("text must be 2000 characters or fewer")
    try:
        profile_snapshot = db.collection("users").document(user_id).get()
        username = (profile_snapshot.to_dict() or {}).get("username", "") if profile_snapshot.exists else ""
        comment_ref = db.collection("videos").document(video_id).collection("comments").document()
        comment_ref.set({"user_id": user_id, "username": username, "text": text, "created_at": firestore.SERVER_TIMESTAMP})
        saved = record_event(db, "comment", user_id, video_id, {"comment_id": comment_ref.id}, data.get("event_id") or comment_ref.id)
        return jsonify({"success": True, "comment_id": comment_ref.id, "duplicate": not saved})
    except Exception:
        logger.exception("Comment submission failed")
        return _error("Unable to save comment", 500)


@app.route("/view/<video_id>", methods=["POST"])
def add_view(video_id):
    data = _json_body()
    user_id, error = _optional_authenticated_user_id()
    if error:
        return error
    return _event_response("view", user_id, video_id, event_id=data.get("event_id"))


@app.route("/like/<video_id>", methods=["POST"])
@_require_auth
def add_like(video_id):
    data = _json_body()
    return _event_response("like", g.auth_user_id, video_id, event_id=data.get("event_id"))


@app.route("/share/<video_id>", methods=["POST"])
@_require_auth
def add_share(video_id):
    data = _json_body()
    return _event_response("share", g.auth_user_id, video_id, event_id=data.get("event_id"))


@app.route("/watch/<video_id>", methods=["POST"])
def add_watch(video_id):
    data = _json_body()
    user_id, error = _optional_authenticated_user_id()
    if error:
        return error
    seconds = data.get("seconds", 0)
    payload = {"seconds": seconds, "duration_seconds": data.get("duration_seconds", max(float(seconds or 0), 1.0))}
    return _event_response("watch", user_id, video_id, payload, data.get("event_id"))


@app.route("/skip/<video_id>", methods=["POST"])
@_require_auth
def add_skip(video_id):
    data = _json_body()
    return _event_response("skip", g.auth_user_id, video_id, event_id=data.get("event_id"))


@app.route("/follow/<creator_id>", methods=["POST"])
@app.route("/follow", methods=["POST"])
@_require_auth
def add_follow(creator_id=None):
    data = _json_body()
    creator_id = creator_id or str(data.get("creator_id", "")).strip()
    if not creator_id:
        return _error("creator_id is required")
    return _event_response("follow", g.auth_user_id, payload={"creator_id": creator_id}, event_id=data.get("event_id"))


@app.route("/unfollow", methods=["POST"])
@_require_auth
def remove_follow():
    if db is None:
        return _error("Firebase not connected", 503)
    creator_id = str(_json_body().get("creator_id", "")).strip()
    if not creator_id:
        return _error("creator_id is required")
    user_ref = db.collection("users").document(g.auth_user_id)
    transaction = db.transaction()

    @firestore.transactional
    def _remove(transaction):
        snapshot = user_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        current = data.get("followed_creators") or []
        creators = list(current.keys()) if isinstance(current, dict) else list(current) if isinstance(current, (list, tuple, set)) else []
        if creator_id not in creators:
            return False
        creators.remove(creator_id)
        transaction.set(user_ref, {"followed_creators": creators}, merge=True)
        return True

    try:
        removed = _remove(transaction)
        return jsonify({"success": True, "following": False, "removed": removed})
    except Exception:
        logger.exception("Unfollow failed")
        return _error("Unable to unfollow creator", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
