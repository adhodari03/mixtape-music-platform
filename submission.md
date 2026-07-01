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

### Bug 1 — Streak resets on Sunday (`streak_service.py:73`)

**Root cause:** A stray weekday guard in the increment branch:
```python
elif days_since_last == 1 and today.weekday() != 6:
    user.listening_streak += 1
else:
    user.listening_streak = 1   # ← Sunday lands here and resets
```
When `today` is Sunday (`weekday() == 6`), the condition is `False` even if `days_since_last == 1`, so the `else` branch fires and resets the streak.

**How to reproduce:**
1. Call `update_listening_streak(user, saturday)` — streak becomes 1.
2. Call `update_listening_streak(user, sunday)` — streak resets to 1 instead of becoming 2.
3. Any Sat→Sun consecutive listen hits this path.

**Test:** `test_bug1_streak_resets_on_sunday` — confirmed by `assert user.listening_streak == 1` after the Sunday listen.

---

### Bug 2 — Feed shows yesterday's listeners (`feed_service.py:13`)

**Root cause:** The "recent" cutoff is a rolling 24-hour window:
```python
RECENT_THRESHOLD = timedelta(hours=24)
cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD
```
A friend who listened at 01:00 UTC yesterday is still within the 24-hour window until 01:00 UTC today. A user checking the feed at midnight today will see that friend as "Listening Now" even though they listened on a different calendar date.

**How to reproduce:**
1. Friend listens at `2024-06-15 01:00 UTC` (yesterday).
2. "Now" is `2024-06-16 00:00 UTC` (today, 23 hours later).
3. Cutoff = `2024-06-15 00:00 UTC` — the event at 01:00 is after the cutoff and appears in the feed.
4. A date-based cutoff (today's midnight) would correctly exclude it.

**Test:** `test_bug2_feed_shows_yesterday_listeners` — asserts `listened_at >= cutoff` (event leaks through) and `listened_at < today_midnight` (a correct implementation would block it).

---

### Bug 3 — Duplicate SQL rows in search (`search_service.py:26`)

**Root cause:** The query joins on `song_tags` without `DISTINCT`:
```python
db.session.query(Song)
    .outerjoin(song_tags, Song.id == song_tags.c.song_id)
    .filter(...)
    .all()
```
A song with N tags produces N SQL rows (one per tag join). SQLAlchemy 2.0's session identity map collapses these back to one ORM object before returning from `.all()`, so the bug is currently masked at the Python layer. However, the database does the extra work, and the query would produce visible duplicates if used with `select()` (SQLAlchemy 2.0 style) or outside a session context.

**How to reproduce (structural):**
1. Create a song with 3 tags.
2. Run the equivalent raw SQL directly: `SELECT song.id FROM song LEFT OUTER JOIN song_tags ON song.id = song_tags.song_id WHERE ...` → 3 rows returned.
3. Verify ORM `.all()` collapses to 1 — the masked state.

**Test:** `test_bug3_search_duplicate_sql_rows` — asserts `len(raw_rows) == 3` (database-level duplicate confirmed) and `len(orm_results) == 1` (ORM masks it today).

---

### Bug 4 — No notification when a song is rated (`notification_service.py:73`)

**Root cause:** `rate_song()` saves the `Rating` and commits, but never calls `create_notification()`. The parallel code path `add_to_playlist()` does notify correctly — `rate_song()` is simply missing the equivalent block.

```python
def rate_song(user_id, song_id, score):
    ...
    db.session.commit()
    return rating          # ← no create_notification() call here
```

**How to reproduce:**
1. User A shares a song.
2. User B calls `rate_song(B.id, song.id, 5)`.
3. Query `get_notifications(A.id)` → empty list.
4. Expected: a `song_rated` notification for User A.

**Test:** `test_bug4_no_notification_on_rating` — asserts `len(notifications) == 0` to confirm no notification was created.

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
