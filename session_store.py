"""
session_store.py — durable, multi-worker-safe storage for ReviewSession state

Flask requests are stateless: a ReviewSession created in one request (e.g.
POST /generate) has to survive until a later request (GET /review/<id>,
then whatever approve/remove clicks follow) picks it back up — possibly
handled by a different worker process, possibly after a server restart.
A plain in-memory dict doesn't survive either of those, which matters once
"usable by someone other than the developer" is a real goal: two staff
members hitting the app from different devices, or the process restarting
mid-review, shouldn't lose work.

Backed by SQLite (stdlib, no new dependency) rather than one-JSON-file-per-
session — a single file with atomic upserts is both simpler to reason
about and safer under concurrent writes than a directory of loose files.

review.py's ReviewSession itself stays pure in-memory logic, independently
testable without any of this — this module only knows how to serialize its
state to and from SQLite via ReviewSession.items() and Candidate/MatchResult's
fields.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from resolver import Candidate, MatchResult
from review import ReviewSession

DEFAULT_DB_PATH = "review_sessions.db"
DEFAULT_PRUNE_AFTER_DAYS = 7   # a generate() nobody ever finalized is almost certainly abandoned


def _match_result_to_dict(result: MatchResult) -> dict:
    return asdict(result)   # asdict recurses into the nested Candidate dataclass


def _match_result_from_dict(d: dict) -> MatchResult:
    d = dict(d)
    candidate = Candidate(**d.pop("candidate"))
    return MatchResult(candidate=candidate, **d)


class SessionStore:
    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")   # better concurrent read/write behavior
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_sessions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    target_json TEXT
                )
                """
            )
            conn.commit()

    def create(self, tracks: list[MatchResult], target: Optional[dict] = None) -> str:
        """
        Create a new session from a fresh track list (all PENDING) and
        return its id. Raises whatever ReviewSession's constructor raises
        (duplicate/missing track_id) before anything touches the database.

        `target` is optional, freeform metadata about where this batch is
        headed (e.g. {"action": "append", "playlist_id": ..., "playlist_name":
        ...}) — set once at creation, fetched via get_target(), and used to
        keep the finalize form's defaults consistent across every review
        action instead of only the first page view. It's advisory: finalize()
        still honors whatever the submitted form says, this just seeds it.
        """
        session = ReviewSession(tracks)   # validates up front
        session_id = uuid.uuid4().hex
        self._write(session_id, session, is_new=True, target=target)
        return session_id

    def get_target(self, session_id: str) -> Optional[dict]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT target_json FROM review_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return json.loads(row[0])

    def load(self, session_id: str) -> Optional[ReviewSession]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT items_json FROM review_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None

        items = json.loads(row[0])
        tracks = [_match_result_from_dict(entry["result"]) for entry in items]
        session = ReviewSession(tracks)
        for entry in items:
            track_id = entry["result"]["track_id"]
            session._items[track_id].status = entry["status"]
            session._items[track_id].note = entry["note"]
        return session

    def save(self, session_id: str, session: ReviewSession) -> None:
        """Persist a session's current state. Raises KeyError (via a plain
        lookup miss becoming a no-op update) — callers should load() first
        to confirm the id exists; save() on a nonexistent id is a silent
        no-op by SQL UPDATE semantics, not an error, so check load()'s
        return value if that distinction matters to you."""
        self._write(session_id, session, is_new=False)

    def delete(self, session_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM review_sessions WHERE id = ?", (session_id,))
            conn.commit()

    def prune(self, older_than_days: int = DEFAULT_PRUNE_AFTER_DAYS) -> int:
        """Delete sessions whose updated_at is older than the cutoff — a
        generate() nobody ever came back to finalize or even touch again.
        Uses updated_at, not created_at, so a session someone is actively
        reviewing (approving/removing tracks, which calls save()) never
        gets swept out from under them mid-review. Returns count removed.
        Cheap enough to call opportunistically (e.g. once per index() GET)
        rather than needing a separate scheduler process."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with closing(self._connect()) as conn:
            cursor = conn.execute("DELETE FROM review_sessions WHERE updated_at < ?", (cutoff,))
            conn.commit()
            return cursor.rowcount

    def _write(self, session_id: str, session: ReviewSession, *, is_new: bool,
               target: Optional[dict] = None) -> None:
        payload = json.dumps([
            {
                "result": _match_result_to_dict(item.result),
                "status": item.status,
                "note": item.note,
            }
            for item in session.items()
        ])
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            if is_new:
                conn.execute(
                    "INSERT INTO review_sessions (id, created_at, updated_at, items_json, target_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, now, now, payload, json.dumps(target) if target is not None else None),
                )
            else:
                # target is intentionally not touched here — it's set once at
                # create() and stays fixed for the session's lifetime.
                conn.execute(
                    "UPDATE review_sessions SET updated_at = ?, items_json = ? WHERE id = ?",
                    (now, payload, session_id),
                )
            conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Demo — runs fully offline
# ─────────────────────────────────────────────────────────────────────────────

def _demo():
    import tempfile

    from resolver import Candidate as _Candidate

    def mk(id_, title, artist):
        return MatchResult(
            candidate=_Candidate(title, artist), accepted=True, reason="ok",
            track_id=id_, track_uri=f"spotify:track:{id_}",
            track_name=title, track_artists=artist, score=1.0,
        )

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.db")

        session_id = store.create([mk("t1", "Ain't No Sunshine", "Bill Withers"),
                                    mk("t2", "Lean On Me", "Bill Withers")])
        print(f"Created session {session_id}")

        session = store.load(session_id)
        session.approve("t1")
        session.remove("t2", reason="too obvious")
        store.save(session_id, session)

        reloaded = store.load(session_id)   # simulates a different worker process
        print(f"Reloaded: approved={[i.result.track_id for i in reloaded.items() if i.status == 'approved']}, "
              f"removed={[i.result.track_id for i in reloaded.items() if i.status == 'removed']}")


if __name__ == "__main__":
    _demo()
