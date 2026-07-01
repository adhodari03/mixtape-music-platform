# Mixtape — Technical Codebase Map & Bug Reproduction

## Architecture Overview

Mixtape is a Flask + SQLAlchemy 2.0 application (`app.py`) backed by SQLite. It has three layers:

- **Routes** (`routes/`) — four Flask blueprints (`songs`, `playlists`, `users`, `feed`). Each blueprint is thin: it parses HTTP input, calls a service, and returns JSON.
- **Services** (`services/`) — all business logic lives here across five files: `streak_service`, `feed_service`, `search_service`, `notification_service`, `playlist_service`.
- **Models** (`models.py`) — six SQLAlchemy ORM classes plus three association tables.

## Database Schema

| Model | Key fields | Relationships |
|---|---|---|
| `User` | `listening_streak`, `last_listened_at` | Self-referential M2M via `friendships`; owns `Song`, `Rating`, `ListeningEvent`, `Notification`, `Playlist` |
| `Song` | `title`, `artist`, `shared_by` (FK→User) | M2M with `Tag` via `song_tags`; M2M with `Playlist` via `playlist_entries` (has `position` column) |
| `ListeningEvent` | `user_id`, `song_id`, `listened_at` | Append-only audit log of every listen |
| `Rating` | `user_id`, `song_id`, `score` (1–5) | Unique constraint on `(user_id, song_id)` |
| `Playlist` | `created_by`, `is_collaborative` | Songs ordered by `playlist_entries.position` |
| `Notification` | `user_id`, `notification_type`, `body`, `read` | Created by service layer on social events |

---

## Bug Reproduction Breakdown

All reproductions live in [`tests/reproduce_all_bugs.py`](tests/reproduce_all_bugs.py). Run with:
```
python -m pytest tests/reproduce_all_bugs.py -v
```
All 5 tests pass, each asserting the buggy state to confirm the defect exists.

---

### Bug 1 — My listening streak keeps resetting (`streak_service.py`)

**Issue:** Users who listen on Saturday and then Sunday find their streak resets to 1 instead of continuing.

**How I reproduced it:**
The existing test `test_streak_increments_on_sunday` in `tests/test_streaks.py` was already failing. I confirmed it manually by calling `update_listening_streak(user, saturday)` then `update_listening_streak(user, sunday)` in a test app context. Saturday correctly set the streak to 1. The Sunday call returned a streak of 1 — a reset — instead of 2. The reproduction test `test_bug1_streak_resets_on_sunday` in `tests/reproduce_all_bugs.py` pins this exact sequence.

**How I found the root cause:**
I opened `services/streak_service.py` and went directly to `update_listening_streak()`, since that is the only function that mutates `user.listening_streak`. The logic has three branches on `days_since_last`. I read the middle branch — the increment branch — at line 73 and saw it had a compound condition: `days_since_last == 1 and today.weekday() != 6`. The `weekday()` guard was the only thing that could reject a valid consecutive-day listen, so I was immediately confident this was the cause.

**Root cause:**
Python's `datetime.date.weekday()` returns `6` for Sunday. The streak increment branch at `streak_service.py:73` was written as `days_since_last == 1 and today.weekday() != 6`. When a user listens on Sunday (`weekday() == 6`), the second part of the condition evaluates to `False`, making the whole `elif` `False` even though exactly one day has passed since Saturday. Python then falls through to the `else` branch, which unconditionally resets the streak to 1. No such weekday check belongs in the increment logic at all — the only thing that should gate an increment is whether exactly one calendar day has elapsed.

**Fix and side-effect check:**
Removed the `and today.weekday() != 6` guard entirely, leaving the branch as `elif days_since_last == 1:`. This makes Sunday behave identically to any other day of the week. After the fix, all five tests in `tests/test_streaks.py` pass (start at 1, increment on consecutive, no double-count same day, reset after skipped day, and the previously-failing Sunday increment). No other service reads or writes `listening_streak` directly, so there are no adjacent call sites to check.

---

### Bug 2 — Friends Listening Now shows people from yesterday (`feed_service.py`)

**Issue:** The "Friends Listening Now" feed shows friends who listened yesterday, not just today.

**How I reproduced it:**
I constructed the sharpest possible edge case: a friend listens at 23:00 UTC on June 15, and the current user checks the feed at 00:00 UTC on June 16 — only one hour later, but a different calendar day. With the old logic, that friend appears in "Listening Now." The reproduction test `test_bug2_feed_excludes_yesterday_listeners` pins this scenario. I also confirmed it analytically: `RECENT_THRESHOLD = timedelta(hours=24)` on line 13 reaches back a full rolling day, so any listen within the past 24 clock-hours passes the filter regardless of which date it occurred on.

**How I found the root cause:**
I opened `services/feed_service.py` and looked at how the `cutoff` was computed on line 32: `cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD`. The constant `RECENT_THRESHOLD` on line 13 was `timedelta(hours=24)`. A rolling 24-hour window does not align with calendar-day boundaries — that mismatch is the entire problem. No other part of the function was relevant.

**Root cause:**
`timedelta(hours=24)` computes a cutoff by subtracting exactly 24 clock-hours from the current time. "Friends Listening Now" is a feature whose natural boundary is the current calendar day, not the last 24 hours. When a friend listens at 23:00 UTC and the user checks the feed at 00:05 UTC the next day, only 65 minutes have passed — the event is well inside the 24-hour window and appears in results, even though it happened yesterday. The correct cutoff is today's midnight UTC (`now.replace(hour=0, minute=0, second=0, microsecond=0)`), which is a hard date boundary and cannot accidentally include the previous day.

**Fix and side-effect check:**
Replaced the rolling `timedelta(hours=24)` cutoff with `now.replace(hour=0, minute=0, second=0, microsecond=0)` — today's midnight UTC. Removed the now-unused `RECENT_THRESHOLD` constant and `timedelta` import to keep the module clean. The `get_activity_feed` function in the same file deliberately has no recency filter (it returns the most recent N events regardless of date), so it is unaffected. All 15 tests across streaks, search, and the reproduction suite pass after the change.

---

### Bug 3 — The same song keeps showing up twice in search (`search_service.py`)

**Issue:** Songs with multiple tags produce duplicate rows at the database level due to an unnecessary join, making the query fragile and wasteful.

**How I reproduced it:**
I created a song with 3 tags and ran the exact SQL that the service generates — a `LEFT OUTER JOIN` on `song_tags`. The raw query returned 3 rows, one per tag, for a single song. In the current setup SQLAlchemy 2.0's session identity map collapses those 3 rows back to 1 ORM object before Python sees them, so end users don't see visible duplicates today. But the database is doing 3× the work, and the same query written with SQLAlchemy's `select()` API (the 2.0-style) would return 3 duplicate dicts. The structural defect is confirmed.

**How I found the root cause:**
I opened `services/search_service.py` and read `search_songs()`. The filter only references `Song.title` and `Song.artist` — both columns that live entirely on the `Song` table. There is no reason to touch `song_tags` at all for filtering. The `.outerjoin(song_tags, Song.id == song_tags.c.song_id)` on line 27 is joining a table that contributes nothing to the `WHERE` clause, which immediately told me it was the cause. Tags are already loaded by the `Song.tags` relationship defined in `models.py` as `lazy="subquery"`, so they come back automatically without any manual join.

**Root cause:**
`search_songs()` joined `song_tags` via `outerjoin` without any `DISTINCT` or `GROUP BY`. A song with N tags has N rows in `song_tags`, so the join multiplies the song's row N times in the result set — one row per tag. The join was never needed: the filter conditions (`title` and `artist`) are both on `Song` itself, and the tag data is already fetched automatically by the `lazy="subquery"` relationship on `Song.tags`. The join was purely accidental load — it added cost and risk with no benefit.

**Fix and side-effect check:**
Removed the `.outerjoin(song_tags, ...)` line entirely and cleaned up the now-unused `Tag` and `song_tags` imports. The query now reads directly from `Song` with no joins. Tags still appear correctly in each result dict because `Song.to_dict()` reads `self.tags`, which SQLAlchemy loads via the subquery relationship. All 5 search tests pass, tags are verified present in the reproduction test, and no other service calls `search_songs()`.

---

### Bug 4 — Got notified when a friend added my song to a playlist but not when they rated it (`notification_service.py`)

**Issue:** Rating a song produces no notification for the person who shared it, even though adding that same song to a playlist does.

**How I reproduced it:**
I called `rate_song(rater.id, song.id, 5)` in a test context where `rater` and `sharer` are two different users, then queried `get_notifications(sharer.id)`. The result was an empty list. I then called `add_to_playlist()` in the same conditions and confirmed it does produce a notification — proving the omission is specific to `rate_song`, not a general notification system failure.

**How I found the root cause:**
I read `notification_service.py` top to bottom. `add_to_playlist()` explicitly calls `create_notification()` after committing the playlist change. `rate_song()` commits the `Rating` and immediately `return`s the rating object — there is no `create_notification()` call anywhere in the function. The missing call was the entire problem; no other logic was involved.

**Root cause:**
`rate_song()` in `notification_service.py` was simply never wired up to send a notification. After `db.session.commit()`, the function returned the `Rating` directly. The `create_notification()` helper exists and works — `add_to_playlist()` in the same file uses it correctly — but whoever wrote `rate_song()` left that step out. Because `rate_song()` already has both the `song` object (which carries `song.shared_by`) and the `rater` object (which carries `rater.username`), everything needed to construct the notification message was already in scope.

**Fix and side-effect check:**
Added a `create_notification()` call after `db.session.commit()`, guarded by `song.shared_by != user_id` so a user rating their own shared song does not notify themselves — matching the same guard used in `add_to_playlist()`. The notification fires on every rating submission including re-rates, per the confirmed intended behaviour. The reproduction test covers three scenarios: first rating notifies, re-rating notifies again, and self-rating produces no notification. All other tests remain green.

---

### Bug 5 — Last playlist song silently dropped (`playlist_service.py:66`)

**Root cause:** An off-by-one Python slice on the query result:
```python
return [song.to_dict() for song in songs[:-1]]
```
`songs[:-1]` excludes the last element of the list. For a 5-song playlist ordered by position, Track 5 is always missing. For a 1-song playlist, the result is always empty.

**How to reproduce:**
1. Create a playlist with 5 songs at positions 1–5.
2. Call `get_playlist_songs(playlist.id)`.
3. Result contains 4 songs: Track 1–4. Track 5 (position=5) is gone.

**Test:** `test_bug5_last_song_missing_from_playlist` — asserts `"Track 5" not in titles` and `len(result) == 4` to confirm the drop.
