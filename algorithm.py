"""Recommendation scoring helpers for bounded Firestore candidate sets."""

from datetime import datetime, timezone
import math


FRESHNESS_HALF_LIFE_HOURS = 24 * 7
TRENDING_HALF_LIFE_HOURS = 24
MAX_PERSONALIZATION_BONUS = 0.35


def _number(value):
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _age_hours(video, now=None):
    created_at = _timestamp(video.get("created_at"))
    if not created_at:
        return 24.0
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created_at).total_seconds() / 3600)


def _bayesian_rate(successes, trials, prior_rate):
    return (successes + prior_rate * 20.0) / (trials + 20.0)


def _decay(age_hours, half_life_hours):
    return math.exp(-math.log(2) * age_hours / half_life_hours)


def calculate_trending_score(video, now=None):
    """Apply time decay to the online-updated score; estimate it for legacy records."""
    stored = _number(video.get("trending_score"))
    updated_at = _timestamp(video.get("score_updated_at"))
    if stored and updated_at:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (now - updated_at).total_seconds() / 3600)
        return stored * _decay(elapsed, TRENDING_HALF_LIFE_HOURS)

    views = _number(video.get("views"))
    engagement = (
        _bayesian_rate(_number(video.get("likes")), views, 0.04) * 2.0
        + _bayesian_rate(_number(video.get("comments")), views, 0.01) * 2.5
        + _bayesian_rate(_number(video.get("shares")), views, 0.01) * 3.0
        + _bayesian_rate(_number(video.get("completed_views")), views, 0.25) * 1.5
        - _bayesian_rate(_number(video.get("skipped_views")), views, 0.15) * 1.5
    )
    return math.log1p(views) * max(0.0, engagement) * _decay(_age_hours(video, now), TRENDING_HALF_LIFE_HOURS)


def calculate_score(video, interests=None, followed_creators=None, now=None):
    """Blend completion, engagement, watch depth, freshness, trend, and affinity."""
    views = _number(video.get("views"))
    likes = _number(video.get("likes"))
    comments = _number(video.get("comments"))
    shares = _number(video.get("shares"))
    completions = _number(video.get("completed_views"))
    skips = _number(video.get("skipped_views"))
    duration = _number(video.get("duration_seconds"))
    watch_time = _number(video.get("watch_time", video.get("total_watch_seconds")))

    completion_rate = _number(video.get("completion_rate"))
    if views and not completion_rate and completions:
        completion_rate = completions / views
    completion = _bayesian_rate(completion_rate * views, views, 0.25)
    skip_rate = _number(video.get("skip_rate"))
    if views and not skip_rate and skips:
        skip_rate = skips / views
    skip_quality = _bayesian_rate(skips if skips else skip_rate * views, views, 0.15)

    watch_ratio = min(1.0, watch_time / max(1.0, views * duration)) if duration else 0.0
    quality = (
        completion * 0.38
        + min(1.0, _bayesian_rate(likes, views, 0.04) * 5) * 0.16
        + min(1.0, _bayesian_rate(comments, views, 0.01) * 10) * 0.10
        + min(1.0, _bayesian_rate(shares, views, 0.01) * 8) * 0.16
        + watch_ratio * 0.20
        - skip_quality * 0.25
    )
    age = _age_hours(video, now)
    score = max(0.0, quality) * _decay(age, FRESHNESS_HALF_LIFE_HOURS)
    score += 0.12 / math.sqrt(1.0 + views / 25.0)
    score += math.log1p(calculate_trending_score(video, now)) * 0.035

    interests = interests or {}
    tags = video.get("tags") or []
    if isinstance(tags, str):
        tags = [tag.strip().lower() for tag in tags.split(",") if tag.strip()]
    category = str(video.get("category") or "").strip().lower()
    affinities = [_number(interests.get(str(tag).strip().lower())) for tag in tags]
    if category:
        affinities.append(_number(interests.get(category)))
    affinity = max(affinities, default=0.0)
    score *= 1.0 + min(MAX_PERSONALIZATION_BONUS, affinity * MAX_PERSONALIZATION_BONUS)

    if video.get("creator_id") in (followed_creators or []):
        score *= 1.2
    return round(score, 6)


def rank_videos(videos, interests=None, followed_creators=None, seen_video_ids=None):
    """Personalize and sort only the bounded candidate window supplied by the caller."""
    seen_video_ids = set(seen_video_ids or [])
    ranked = []
    for source in videos:
        video = dict(source)
        video["score"] = calculate_score(video, interests, followed_creators)
        if video.get("video_id") in seen_video_ids:
            video["score"] *= 0.35
        ranked.append(video)
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked
