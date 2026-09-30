"""Firestore event ingestion and watch-session analytics."""

from datetime import datetime, timezone
import hashlib
import math
import uuid

from firebase_admin import firestore


EVENT_WEIGHTS = {
    "view": 0.1,
    "watch": 0.2,
    "skip": -0.25,
    "like": 1.0,
    "comment": 1.5,
    "share": 2.0,
    "follow": 1.5,
}
VIDEO_COUNTERS = {
    "view": "views",
    "like": "likes",
    "comment": "comments",
    "share": "shares",
}
INTEREST_WEIGHTS = {
    "view": 0.05,
    "watch": 0.1,
    "skip": -0.15,
    "like": 1.0,
    "comment": 1.2,
    "share": 1.5,
    "follow": 1.5,
}


def _safe_id(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _as_nonnegative_float(value, field):
    try:
        number = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a non-negative finite number")
    return number


def _interest_keys(video):
    keys = [str(tag).strip().lower() for tag in video.get("tags", []) if str(tag).strip()]
    if isinstance(video.get("tags"), str):
        keys = [tag.strip().lower() for tag in video["tags"].split(",") if tag.strip()]
    category = str(video.get("category") or "").strip().lower()
    if category:
        keys.append(category)
    return list(dict.fromkeys(keys))


def record_event(db, event_type, user_id=None, video_id=None, payload=None, event_id=None):
    """Persist an event once and atomically update aggregate counters/profile signals."""
    if event_type not in EVENT_WEIGHTS:
        raise ValueError("Unsupported event type")
    payload = payload or {}
    event_id = event_id or uuid.uuid4().hex
    event_ref = db.collection("analytics_events").document(_safe_id(event_id))
    video_ref = db.collection("videos").document(video_id) if video_id else None
    user_ref = db.collection("users").document(user_id) if user_id else None
    duration = _as_nonnegative_float(payload.get("duration_seconds", 0), "duration_seconds")
    seconds = _as_nonnegative_float(payload.get("seconds", 0), "seconds")
    completion = min(1.0, seconds / duration) if duration else 0.0
    skipped = event_type == "skip" or (event_type == "watch" and seconds <= 3)
    transaction = db.transaction()

    @firestore.transactional
    def _commit(transaction):
        event_snapshot = event_ref.get(transaction=transaction)
        if event_snapshot.exists:
            return False

        video_snapshot = video_ref.get(transaction=transaction) if video_ref else None
        user_snapshot = user_ref.get(transaction=transaction) if user_ref else None
        video = video_snapshot.to_dict() or {} if video_snapshot and video_snapshot.exists else {}
        profile = user_snapshot.to_dict() or {} if user_snapshot and user_snapshot.exists else {}
        transaction.create(event_ref, {
            "event_id": event_id,
            "event_type": event_type,
            "user_id": user_id,
            "video_id": video_id,
            "payload": payload,
            "created_at": firestore.SERVER_TIMESTAMP,
        })

        if video_ref and video_snapshot and video_snapshot.exists:
            fields = {}
            counter = VIDEO_COUNTERS.get(event_type)
            if counter:
                fields[counter] = firestore.Increment(1)
            if event_type == "watch":
                old_views = max(0, int(video.get("views", 0) or 0))
                old_sessions = max(0, int(video.get("watch_sessions", 0) or 0))
                old_completed = max(0, int(video.get("completed_views", 0) or 0))
                old_skipped = max(0, int(video.get("skipped_views", 0) or 0))
                completion_total = max(0.0, float(video.get("completion_total", 0) or 0)) + completion
                fields["watch_time"] = firestore.Increment(seconds)
                fields["watch_sessions"] = firestore.Increment(1)
                if completion >= 0.8:
                    fields["completed_views"] = firestore.Increment(1)
                    old_completed += 1
                if skipped:
                    fields["skipped_views"] = firestore.Increment(1)
                    old_skipped += 1
                fields["completion_rate"] = completion_total / max(1, old_sessions + 1)
                fields["skip_rate"] = old_skipped / max(1, old_views)
                fields["completion_total"] = firestore.Increment(completion)
            elif event_type == "skip":
                old_views = max(1, int(video.get("views", 0) or 0))
                old_skipped = max(0, int(video.get("skipped_views", 0) or 0)) + 1
                fields["skipped_views"] = firestore.Increment(1)
                fields["skip_rate"] = old_skipped / old_views

            if event_type != "follow":
                prior_score = max(0.0, float(video.get("trending_score", 0) or 0))
                score_at = video.get("score_updated_at")
                if score_at and hasattr(score_at, "timestamp"):
                    elapsed = max(0.0, datetime.now(timezone.utc).timestamp() - score_at.timestamp()) / 3600
                    prior_score *= math.exp(-math.log(2) * elapsed / 24)
                fields["trending_score"] = max(0.0, prior_score + EVENT_WEIGHTS[event_type])
                fields["score_updated_at"] = firestore.SERVER_TIMESTAMP
            transaction.update(video_ref, fields)

        if user_ref:
            updates = {"last_active_at": firestore.SERVER_TIMESTAMP}
            if event_type == "follow" and payload.get("creator_id"):
                updates["followed_creators"] = firestore.ArrayUnion([payload["creator_id"]])
            if video:
                interests = dict(profile.get("interests") or {})
                delta = INTEREST_WEIGHTS[event_type]
                if event_type == "watch" and duration:
                    delta *= completion
                for key in _interest_keys(video):
                    interests[key] = max(0.0, min(100.0, float(interests.get(key, 0) or 0) + delta))
                if interests:
                    updates["interests"] = interests
            transaction.set(user_ref, updates, merge=True)
        return True

    return _commit(transaction)


def create_watch(db, user_id, video_id):
    """Create a session, count its view once, and flag repeated user/video sessions."""
    watch_ref = db.collection("watch_history").document()
    rewatch = False
    if user_id:
        stats_ref = db.collection("user_video_stats").document(_safe_id(f"{user_id}:{video_id}"))
        transaction = db.transaction()

        @firestore.transactional
        def _increment_watch_count(transaction):
            snapshot = stats_ref.get(transaction=transaction)
            data = snapshot.to_dict() or {} if snapshot.exists else {}
            count = int(data.get("watch_count", 0) or 0)
            transaction.set(stats_ref, {"user_id": user_id, "video_id": video_id, "watch_count": count + 1}, merge=True)
            return count > 0

        rewatch = _increment_watch_count(transaction)

    watch_ref.set({
        "user_id": user_id,
        "video_id": video_id,
        "start_time": firestore.SERVER_TIMESTAMP,
        "watch_seconds": 0,
        "completion_rate": 0.0,
        "completed": False,
        "skipped": False,
        "rewatch": rewatch,
        "finished": False,
    })
    record_event(db, "view", user_id, video_id, event_id=f"view:{watch_ref.id}")
    if rewatch:
        db.collection("videos").document(video_id).update({"rewatches": firestore.Increment(1)})
    return watch_ref.id


def finish_watch(db, watch_id, seconds, duration, user_id=None):
    """Close one session and calculate completion from watched/duration seconds."""
    seconds = _as_nonnegative_float(seconds, "seconds")
    duration = _as_nonnegative_float(duration, "duration")
    if duration <= 0:
        raise ValueError("duration must be greater than zero")

    watch_ref = db.collection("watch_history").document(watch_id)
    snapshot = watch_ref.get()
    if not snapshot.exists:
        raise ValueError("Watch session not found")
    watch = snapshot.to_dict() or {}
    if watch.get("finished"):
        return {
            "completion_rate": watch.get("completion_rate", 0.0),
            "skipped": watch.get("skipped", False),
            "completed": watch.get("completed", False),
            "rewatch": watch.get("rewatch", False),
        }

    user_id = user_id or watch.get("user_id")
    video_id = watch.get("video_id")
    completion = min(1.0, seconds / duration)
    skipped = seconds <= min(3.0, duration * 0.1)
    completed = completion >= 0.8
    watch_ref.update({
        "watch_seconds": seconds,
        "duration_seconds": duration,
        "completion_rate": completion,
        "completed": completed,
        "skipped": skipped,
        "finished": True,
        "end_time": firestore.SERVER_TIMESTAMP,
    })
    record_event(db, "watch", user_id, video_id, {
        "seconds": seconds,
        "duration_seconds": duration,
        "watch_id": watch_id,
        "rewatch": bool(watch.get("rewatch")),
    }, event_id=f"finish:{watch_id}")
    return {
        "completion_rate": completion,
        "skipped": skipped,
        "completed": completed,
        "rewatch": bool(watch.get("rewatch")),
    }
