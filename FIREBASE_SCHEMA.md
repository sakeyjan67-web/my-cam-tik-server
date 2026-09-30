# Recommendation Engine: Firebase and Client Contract

## Firestore Collections

### `videos/{video_id}`

Video documents written by `/upload` should use these fields:

| Field | Type | Purpose |
| --- | --- | --- |
| `video_id`, `title`, `file_id`, `filename` | string | Existing upload/feed fields; `file_id` remains the Telegram identifier. |
| `creator_id` | string or null | Creator affinity and follow recommendations. |
| `tags` | string array | Interest signals, normalized to lowercase. |
| `category` | string | Coarse interest signal. |
| `duration_seconds` | number | Required to calculate completion rate accurately. |
| `created_at` | Firestore timestamp | Freshness decay and newest-first legacy fallback. |
| `views`, `likes`, `comments`, `shares` | integer | Lifetime aggregate counts. |
| `watch_time` | number | Total watched seconds. |
| `watch_sessions`, `rewatches` | integer | Session and repeat-watch totals. |
| `completed_views`, `skipped_views` | integer | Sessions at >=80% completion and early skips. |
| `completion_total` | number | Sum of per-session completion fractions. |
| `completion_rate`, `skip_rate` | number | Derived rates in the range 0..1. |
| `trending_score` | number | Online interaction score with exponential decay. |
| `score_updated_at` | Firestore timestamp | Time anchor for trending-score decay. |

Older video records can be read using the `created_at` fallback when no scored candidates are returned. Backfill legacy records with `trending_score: 0.12`, `score_updated_at`, `duration_seconds` when known, and empty `tags`/`category` values before relying on score-ordered retrieval at full scale.

### `users/{user_id}`

`interests` is a map from normalized tag/category to a bounded accumulated weight. `followed_creators` is an array of creator IDs. `last_active_at` stores the last accepted event time. Keep user IDs tied to your authenticated identity provider; do not accept a caller's arbitrary ID as proof of identity in a public deployment.

### `watch_history/{watch_id}`

One document per playback session: `user_id`, `video_id`, `start_time`, `end_time`, `watch_seconds`, `duration_seconds`, `completion_rate`, `completed`, `skipped`, `rewatch`, and `finished`. Recent-history recommendations query by `user_id` and descending `start_time`.

### `user_video_stats/{sha256(user_id:video_id)}`

Stores `user_id`, `video_id`, and `watch_count`; subsequent sessions are marked as rewatches without reading an unbounded history.

### `analytics_events/{sha256(event_id)}`

Idempotent event ledger with `event_id`, `event_type`, `user_id`, optional `video_id`, payload, and server timestamp. Send a stable `event_id` when retrying a request. Aggregates are updated in the same Firestore transaction.

### `videos/{video_id}/comments/{comment_id}`

Comment records include `user_id`, `text`, and server `created_at`. The video-level `comments` field is the aggregate count.

## Required Firestore Indexes

Create the composite indexes if Firestore reports a missing-index link:

- `videos`: `trending_score` descending, `created_at` descending.
- `watch_history`: `user_id` ascending, `start_time` descending.

All recommendation reads are capped at 100 video candidates and 100 recent watch records per request. Ranking happens only inside that bounded candidate window. `/recommendations` returns `next_cursor`; the legacy `/feed` array keeps its response shape and returns the cursor in `X-Next-Cursor`.

## API Endpoints

| Method and path | Contract |
| --- | --- |
| `POST /upload` | Multipart fields: `video`, optional `title`, `creator_id`, comma-separated `tags`, `category`, and `duration_seconds`. Keeps the existing `{success, file_id}` response fields and also returns `video_id`. |
| `GET /feed?user_id=...&limit=50&cursor=...` | Backward-compatible JSON array, now bounded and optionally personalized. Read `X-Next-Cursor` response header for the next `cursor`. `limit` is capped at 100. |
| `GET /recommendations?user_id=...&limit=20&cursor=...` | `{items: [...], next_cursor: string|null}` for paged retrieval. |
| `POST /watch/start` | JSON `{user_id, video_id}`; returns a `watch_id` and records a view. |
| `POST /watch/finish` | JSON `{watch_id, seconds, duration_seconds, user_id?}`; server calculates completion, completed, and skipped values. |
| `POST /events` | JSON `{type, user_id?, video_id?, creator_id?, seconds?, duration_seconds?, event_id?}`. Types: `view`, `watch`, `skip`, `like`, `comment`, `share`, `follow`. |
| `POST /comment/{video_id}` | JSON `{user_id?, text, event_id?}`; persists comment and increments the aggregate. |
| `POST /view/{video_id}`, `/like/{video_id}`, `/share/{video_id}`, `/watch/{video_id}`, `/skip/{video_id}` | Existing tracking routes retained. Add `user_id` and a stable `event_id` to personalize and deduplicate retries. |
| `POST /follow/{creator_id}` | JSON `{user_id, event_id?}`; stores creator affinity. |

## Frontend Event Tracking Requirements

1. Call `/watch/start` when playback actually begins and keep its `watch_id` for that player session.
2. Measure foreground playback seconds only. Pause accounting while paused, buffering, or the page/app is hidden; do not infer watched time from wall-clock time.
3. Call `/watch/finish` on end, navigation away, or a deliberate skip, sending measured seconds and the media's true duration. Use `sendBeacon` or a keepalive request for page teardown where supported, and periodically flush watch progress for long sessions.
4. Let the server calculate `completion_rate = min(seconds / duration_seconds, 1)`. Do not send a client-computed completion rate as authoritative data.
5. Send `like`, `comment`, `share`, and `follow` only after the user action succeeds. Supply stable event IDs across retries; use the comment endpoint for comment text.
6. Include stable authenticated `user_id` values and accurate video metadata (`creator_id`, tags, category, duration) so watch history and interest profiles can be joined.
7. Keep player/UI behavior on the existing app interface. This change does not edit or replace `templates/index.html`.

## Runtime Configuration

Set `BOT_TOKEN`, `CHANNEL_ID`, and `FIREBASE_CREDENTIALS` in deployment secrets. `FIREBASE_CREDENTIALS` is a JSON service-account object; never commit or send the credential file to the client. For local credential-file setup, place `firebase_credentials.json` beside `app.py`. `UPLOAD_FOLDER` and `MAX_UPLOAD_BYTES` can be configured through environment variables.
