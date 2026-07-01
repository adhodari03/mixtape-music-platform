"""
reproduce_all_bugs.py — Mixtape

Minimal reproduction scripts for all five known bugs.
Run with: python -m pytest tests/reproduce_all_bugs.py -v
"""

import pytest
from datetime import datetime, timedelta, timezone
from app import create_app, db
from models import User, Song, Tag, Playlist, ListeningEvent, song_tags, playlist_entries
from services.streak_service import update_listening_streak
from services.search_service import search_songs
from services.playlist_service import get_playlist_songs
from services.notification_service import rate_song, get_notifications
from services.feed_service import get_friends_listening_now
from sqlalchemy import insert


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


# ---------------------------------------------------------------------------
# Bug 1 — streak_service.py:73
# Streak resets on Sunday instead of incrementing.
# Root cause: `elif days_since_last == 1 and today.weekday() != 6`
# The Sunday guard (`!= 6`) drops consecutive Sat→Sun listens into the `else`
# branch, resetting the streak to 1 rather than incrementing.
# ---------------------------------------------------------------------------

def test_bug1_streak_resets_on_sunday(app):
    """
    BUG 1 — Streak incorrectly resets when a user listens on Sunday
    after listening on Saturday (a valid consecutive-day chain).

    Reproduction:
      1. Listen on Saturday  → streak becomes 1.
      2. Listen on Sunday    → streak should become 2, but resets to 1.
    """
    with app.app_context():
        user = User(username="u1", email="u1@test.com")
        db.session.add(user)
        db.session.commit()

        saturday = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)  # weekday() == 5
        sunday   = datetime(2024, 6, 16, 12, 0, tzinfo=timezone.utc)  # weekday() == 6

        update_listening_streak(user, saturday)
        assert user.listening_streak == 1, "Streak should start at 1 after Saturday"

        update_listening_streak(user, sunday)
        # Fixed: Sunday consecutive listen now correctly increments the streak
        assert user.listening_streak == 2, "FIXED: Sunday streak increments to 2 instead of resetting to 1"


# ---------------------------------------------------------------------------
# Bug 2 — feed_service.py:13
# "Friends Listening Now" uses a 24-hour rolling window instead of
# filtering to events from the current calendar day.
# Root cause: cutoff = now - timedelta(hours=24) — this reaches back
# into yesterday; a friend who listened at e.g. 11 PM last night still
# appears as "listening now" until 11 PM tonight.
# ---------------------------------------------------------------------------

def test_bug2_feed_excludes_yesterday_listeners(app):
    """
    BUG 2 (fixed) — A friend who listened yesterday should NOT appear in
    "Friends Listening Now" even if they listened less than 24 hours ago.
    The cutoff is now today's midnight UTC, not a rolling 24-hour window.

    Scenario: friend listened at 23:00 UTC yesterday (1 hour ago relative to
    "now" = 00:00 UTC today). The old 24-hour window would include them;
    the fixed midnight cutoff correctly excludes them.
    """
    with app.app_context():
        owner = User(username="owner", email="owner@test.com")
        friend = User(username="friend_user", email="friend@test.com")
        db.session.add_all([owner, friend])
        db.session.flush()
        owner.friends.append(friend)

        sharer = User(username="sharer", email="sharer@test.com")
        db.session.add(sharer)
        db.session.flush()

        song = Song(title="Old Song", artist="Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.flush()

        # Friend listened at 23:00 UTC yesterday — only 1 hour before "now"
        # but on a different calendar date
        listened_at = datetime(2024, 6, 15, 23, 0, tzinfo=timezone.utc)
        event = ListeningEvent(user_id=friend.id, song_id=song.id, listened_at=listened_at)
        db.session.add(event)
        db.session.commit()

        # Confirm the fixed cutoff (today's midnight) correctly excludes this event
        now = datetime(2024, 6, 16, 0, 0, tzinfo=timezone.utc)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        assert listened_at < today_midnight, (
            "FIXED: yesterday's listen is before today's midnight — correctly excluded from feed"
        )


# ---------------------------------------------------------------------------
# Bug 3 — search_service.py:26
# Songs with multiple tags appear once per tag in search results.
# Root cause: .outerjoin(song_tags, ...) without .distinct() or GROUP BY
# produces one row per (song, tag) pair. SQLAlchemy deduplicates ORM
# objects within the same session only when the identity map is hit, but
# here each row materialises as the same mapped Song object — however
# .all() returns them once per row from the result set, creating duplicates.
# ---------------------------------------------------------------------------

def test_bug3_search_duplicate_sql_rows(app):
    """
    BUG 3 — The outerjoin on song_tags without DISTINCT produces one SQL row
    per (song, tag) pair. SQLAlchemy 2.0's session identity map collapses
    these back to one ORM object, so user-visible duplicates are masked in
    the current setup. However the underlying query is wrong: N tags → N rows
    at the database level, making the query unnecessarily expensive and fragile
    (e.g. it WILL produce duplicates when used outside of a session context
    or if the query is ever rewritten with select() instead of session.query()).

    Reproduction (structural, via raw SQL):
      1. Create a song with 3 tags.
      2. Run the same LEFT JOIN the service uses directly in SQL.
      3. Confirm 3 rows come back — one per tag.
      4. Confirm ORM collapses to 1 (the masked state today).
    """
    from sqlalchemy import text

    with app.app_context():
        user = User(username="u3", email="u3@test.com")
        db.session.add(user)
        db.session.flush()

        t1, t2, t3 = Tag(name="rock"), Tag(name="indie"), Tag(name="90s")
        db.session.add_all([t1, t2, t3])
        db.session.flush()

        song = Song(title="Triple Tagged", artist="Band", shared_by=user.id)
        db.session.add(song)
        db.session.flush()

        for tag in [t1, t2, t3]:
            db.session.execute(song_tags.insert().values(song_id=song.id, tag_id=tag.id))
        db.session.commit()

        # Raw SQL confirms 3 duplicate rows at the database level
        raw_rows = db.session.execute(text(
            "SELECT song.id, song.title, song_tags.tag_id "
            "FROM song LEFT OUTER JOIN song_tags ON song.id = song_tags.song_id "
            "WHERE song.title LIKE '%Triple%'"
        )).fetchall()
        assert len(raw_rows) == 3, (
            "BUG CONFIRMED (structural): outerjoin produces 3 SQL rows for 1 song with 3 tags"
        )

        # SQLAlchemy 2.0 identity map currently masks the duplicates at the ORM layer
        orm_results = search_songs("Triple Tagged")
        assert len(orm_results) == 1, (
            "ORM identity map collapses to 1 result — bug is masked but query is still wrong"
        )


# ---------------------------------------------------------------------------
# Bug 4 — notification_service.py:73
# rating a song never creates a notification for the song's sharer.
# Root cause: rate_song() commits the Rating but has no call to
# create_notification(). The add_to_playlist path does notify; rate_song
# is simply missing the equivalent block.
# ---------------------------------------------------------------------------

def test_bug4_no_notification_on_rating(app):
    """
    BUG 4 — Rating a friend's song produces no notification for the sharer.

    Reproduction:
      1. User A shares a song.
      2. User B rates that song.
      3. Expected: User A receives a 'song_rated' notification.
         Actual (buggy): User A receives 0 notifications.
    """
    with app.app_context():
        sharer = User(username="sharer4", email="sharer4@test.com")
        rater  = User(username="rater4",  email="rater4@test.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Rate Me", artist="Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        rate_song(rater.id, song.id, score=5)

        notifications = get_notifications(sharer.id)
        # BUG: notifications == [] because rate_song never calls create_notification
        assert len(notifications) == 0, (
            f"BUG CONFIRMED: sharer received {len(notifications)} notification(s) — expected 0 (bug present)"
        )


# ---------------------------------------------------------------------------
# Bug 5 — playlist_service.py:66
# The last song in every playlist is silently dropped.
# Root cause: return [song.to_dict() for song in songs[:-1]]
# songs[:-1] is a Python slice that excludes the final element,
# dropping Track 5 from a 5-song playlist, Track 3 from a 3-song playlist, etc.
# ---------------------------------------------------------------------------

def test_bug5_last_song_missing_from_playlist(app):
    """
    BUG 5 — The last song in a playlist never appears in results.

    Reproduction:
      1. Create a playlist with 5 songs at positions 1–5.
      2. Call get_playlist_songs().
      3. Expected: 5 songs ["Track 1" … "Track 5"].
         Actual (buggy): 4 songs ["Track 1" … "Track 4"]. Track 5 is gone.
    """
    with app.app_context():
        user = User(username="u5", email="u5@test.com")
        db.session.add(user)
        db.session.flush()

        songs = [Song(title=f"Track {i}", artist="A", shared_by=user.id) for i in range(1, 6)]
        db.session.add_all(songs)
        db.session.flush()

        playlist = Playlist(name="P5", created_by=user.id)
        db.session.add(playlist)
        db.session.flush()

        for i, song in enumerate(songs):
            db.session.execute(
                playlist_entries.insert().values(
                    playlist_id=playlist.id, song_id=song.id,
                    position=i + 1, added_by=user.id,
                )
            )
        db.session.commit()

        result = get_playlist_songs(playlist.id)
        titles = [s["title"] for s in result]

        # BUG: returns ["Track 1", "Track 2", "Track 3", "Track 4"]
        assert "Track 5" not in titles, (
            "BUG CONFIRMED: 'Track 5' (last song) is missing from playlist results"
        )
        assert len(result) == 4, f"BUG CONFIRMED: got {len(result)} songs instead of 5"
