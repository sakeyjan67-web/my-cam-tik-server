"""Flask API for video upload, feed delivery, and recommendation analytics."""

import json
import hashlib
import hmac
import logging
import math
import os
import time
import uuid
from urllib.parse import quote, urlencode

import requests
import firebase_admin
from google.api_core.exceptions import FailedPrecondition
from firebase_admin import credentials, firestore
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
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


@app.route("/upload", methods=["POST"])
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
    creator_id = request.form.get("creator_id", "").strip() or None
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
        response = jsonify(items)
        response.headers["X-Next-Cursor"] = next_cursor or ""
        return response
    except (TypeError, ValueError):
        return _error("limit must be an integer")
    except Exception:
        logger.exception("Feed request failed")
        return _error("Unable to load feed", 500)


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
def start_watch():
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    user_id = str(data.get("user_id", "")).strip()
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
def finish_watch_route():
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    watch_id = str(data.get("watch_id", "")).strip()
    if not watch_id:
        return _error("watch_id is required")
    try:
        result = finish_watch(db, watch_id, data.get("seconds", 0), data.get("duration_seconds", 0), data.get("user_id"))
        return jsonify({"success": True, **result})
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("Could not finish watch session")
        return _error("Unable to finish watch session", 500)


@app.route("/events", methods=["POST"])
def events():
    data = _json_body()
    event_type = str(data.get("type", "")).strip().lower()
    user_id = str(data.get("user_id", "")).strip() or None
    video_id = str(data.get("video_id", "")).strip() or None
    if event_type not in {"view", "watch", "skip", "like", "comment", "share", "follow"}:
        return _error("Unsupported event type")
    if event_type != "follow" and not video_id:
        return _error("video_id is required for this event")
    if event_type == "follow" and (not user_id or not data.get("creator_id")):
        return _error("user_id and creator_id are required for follow events")
    payload = {key: data[key] for key in ("seconds", "duration_seconds", "creator_id") if key in data}
    return _event_response(event_type, user_id, video_id, payload, data.get("event_id"))


@app.route("/comment/<video_id>", methods=["POST"])
def add_comment(video_id):
    if db is None:
        return _error("Firebase not connected", 503)
    data = _json_body()
    user_id = str(data.get("user_id", "")).strip() or None
    text = str(data.get("text", "")).strip()
    if not text:
        return _error("text is required")
    if len(text) > 2000:
        return _error("text must be 2000 characters or fewer")
    try:
        comment_ref = db.collection("videos").document(video_id).collection("comments").document()
        comment_ref.set({"user_id": user_id, "text": text, "created_at": firestore.SERVER_TIMESTAMP})
        saved = record_event(db, "comment", user_id, video_id, {"comment_id": comment_ref.id}, data.get("event_id") or comment_ref.id)
        return jsonify({"success": True, "comment_id": comment_ref.id, "duplicate": not saved})
    except Exception:
        logger.exception("Comment submission failed")
        return _error("Unable to save comment", 500)


@app.route("/view/<video_id>", methods=["POST"])
def add_view(video_id):
    data = _json_body()
    return _event_response("view", data.get("user_id"), video_id, event_id=data.get("event_id"))


@app.route("/like/<video_id>", methods=["POST"])
def add_like(video_id):
    data = _json_body()
    return _event_response("like", data.get("user_id"), video_id, event_id=data.get("event_id"))


@app.route("/share/<video_id>", methods=["POST"])
def add_share(video_id):
    data = _json_body()
    return _event_response("share", data.get("user_id"), video_id, event_id=data.get("event_id"))


@app.route("/watch/<video_id>", methods=["POST"])
def add_watch(video_id):
    data = _json_body()
    seconds = data.get("seconds", 0)
    payload = {"seconds": seconds, "duration_seconds": data.get("duration_seconds", max(float(seconds or 0), 1.0))}
    return _event_response("watch", data.get("user_id"), video_id, payload, data.get("event_id"))


@app.route("/skip/<video_id>", methods=["POST"])
def add_skip(video_id):
    data = _json_body()
    return _event_response("skip", data.get("user_id"), video_id, event_id=data.get("event_id"))


@app.route("/follow/<creator_id>", methods=["POST"])
def add_follow(creator_id):
    data = _json_body()
    user_id = str(data.get("user_id", "")).strip()
    if not user_id:
        return _error("user_id is required")
    return _event_response("follow", user_id, payload={"creator_id": creator_id}, event_id=data.get("event_id"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
