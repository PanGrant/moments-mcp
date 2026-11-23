"""SQLite storage for the Digest feature (DIGEST_DESIGN.md §2).

Single-file DB at data/digest.db (WAL mode). All timestamp columns are
UTC strings via SQLite's datetime('now'). JSON columns (keywords,
thread_json, matched_interests) are TEXT; helpers parse them at the edges.
"""
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .config import project_root

DEFAULT_DB_PATH = os.path.join(project_root(), "data", "digest.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    keywords TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_msg_id TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '',
    posted_at_approx TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    media_desc TEXT NOT NULL DEFAULT '',
    thread_json TEXT NOT NULL DEFAULT '[]',
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    sponsored INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, source_msg_id)
);
CREATE TABLE IF NOT EXISTS digest_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id INTEGER NOT NULL REFERENCES raw_items(id),
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    matched_interests TEXT NOT NULL DEFAULT '[]',
    score REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_item_id INTEGER NOT NULL UNIQUE REFERENCES digest_items(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS thread_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_item_id INTEGER NOT NULL REFERENCES digest_items(id),
    channel TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open (and initialize) the digest DB. Row access by column name."""
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight migrations for DBs created before a column existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_items)")}
    if "posted_at_approx" not in cols:
        conn.execute("ALTER TABLE raw_items ADD COLUMN posted_at_approx "
                     "TEXT NOT NULL DEFAULT ''")
        conn.commit()
    # sponsored = the post's author row carries the service-account
    # dropdown arrow (自主批阅 skips these) — set at ingest by the arrow
    # probe; pre-existing rows stay 0 unless backfilled by hand.
    if "sponsored" not in cols:
        conn.execute("ALTER TABLE raw_items ADD COLUMN sponsored "
                     "INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    _backfill_posted_at_approx(conn)


def _backfill_posted_at_approx(conn: sqlite3.Connection) -> None:
    """Fill posted_at_approx for rows collected before the column existed."""
    from .time_hints import approx_posted_at
    rows = conn.execute(
        "SELECT id, posted_at FROM raw_items "
        "WHERE posted_at != '' AND posted_at_approx = ''").fetchall()
    for row in rows:
        approx = approx_posted_at(row["posted_at"])
        if approx:
            conn.execute("UPDATE raw_items SET posted_at_approx = ? WHERE id = ?",
                         (approx, row["id"]))
    if rows:
        conn.commit()


# ---------------------------------------------------------------------------
# interests
# ---------------------------------------------------------------------------

def add_interest(conn: sqlite3.Connection, name: str, keywords: List[str] = None,
                 description: str = "", enabled: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO interests (name, keywords, description, enabled) VALUES (?, ?, ?, ?)",
        (name, json.dumps(keywords or [], ensure_ascii=False), description, int(enabled)),
    )
    conn.commit()
    return cur.lastrowid


def list_interests(conn: sqlite3.Connection, enabled_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM interests"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    rows = conn.execute(sql).fetchall()
    return [_interest_to_dict(r) for r in rows]


def update_interest(conn: sqlite3.Connection, interest_id: int, **fields) -> bool:
    allowed = {"name", "keywords", "description", "enabled"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"cannot update interest field: {k}")
        if k == "keywords":
            v = json.dumps(v or [], ensure_ascii=False)
        if k == "enabled":
            v = int(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return False
    vals.append(interest_id)
    cur = conn.execute(f"UPDATE interests SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return cur.rowcount > 0


def delete_interest(conn: sqlite3.Connection, interest_id: int) -> bool:
    cur = conn.execute("DELETE FROM interests WHERE id = ?", (interest_id,))
    conn.commit()
    return cur.rowcount > 0


def _interest_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["keywords"] = json.loads(d["keywords"] or "[]")
    d["enabled"] = bool(d["enabled"])
    return d


# ---------------------------------------------------------------------------
# raw_items
# ---------------------------------------------------------------------------

def insert_raw_item(conn: sqlite3.Connection, item: Dict[str, Any]) -> Tuple[int, bool]:
    """Insert a raw item; returns (id, is_new). Dedupe key is
    (source, source_msg_id) — a repeated post is not ingested twice."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_items "
        "(source, source_msg_id, author, posted_at, posted_at_approx, text, "
        "media_desc, thread_json, sponsored) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["source"], item["source_msg_id"], item.get("author", ""),
            item.get("posted_at", ""), item.get("posted_at_approx", ""),
            item.get("text", ""),
            item.get("media_desc", ""),
            json.dumps(item.get("thread") or [], ensure_ascii=False),
            int(item.get("sponsored", 0) or 0),
        ),
    )
    if cur.rowcount == 1:
        conn.commit()
        return cur.lastrowid, True
    row = conn.execute(
        "SELECT id FROM raw_items WHERE source = ? AND source_msg_id = ?",
        (item["source"], item["source_msg_id"]),
    ).fetchone()
    return row["id"], False


def is_known_raw(conn: sqlite3.Connection, source: str, source_msg_id: str) -> bool:
    """Whether this (source, source_msg_id) was ingested before — the
    collector uses it to detect the 'last seen' boundary while scrolling."""
    row = conn.execute(
        "SELECT 1 FROM raw_items WHERE source = ? AND source_msg_id = ? LIMIT 1",
        (source, source_msg_id),
    ).fetchone()
    return row is not None


def get_raw_item(conn: sqlite3.Connection, raw_item_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM raw_items WHERE id = ?", (raw_item_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["thread"] = json.loads(d.pop("thread_json") or "[]")
    return d


def get_raw_items_without_digest(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Raw items that never produced a digest item (failed summarize runs);
    the pipeline retries these on the next round."""
    rows = conn.execute(
        "SELECT r.* FROM raw_items r "
        "LEFT JOIN digest_items d ON d.raw_item_id = r.id WHERE d.id IS NULL"
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["thread"] = json.loads(d.pop("thread_json") or "[]")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# digest_items
# ---------------------------------------------------------------------------

def insert_digest_item(conn: sqlite3.Connection, raw_item_id: int, title: str,
                       summary: str, matched_interests: List[Dict[str, Any]],
                       score: float) -> int:
    cur = conn.execute(
        "INSERT INTO digest_items (raw_item_id, title, summary, matched_interests, score) "
        "VALUES (?, ?, ?, ?, ?)",
        (raw_item_id, title, summary,
         json.dumps(matched_interests, ensure_ascii=False), float(score)),
    )
    conn.commit()
    return cur.lastrowid


def has_digest(conn: sqlite3.Connection, raw_item_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM digest_items WHERE raw_item_id = ? LIMIT 1", (raw_item_id,)
    ).fetchone()
    return row is not None


def _digest_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["matched_interests"] = json.loads(d["matched_interests"] or "[]")
    return d


def list_digest_items(conn: sqlite3.Connection, source: Optional[str] = None,
                      interest_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Digest items newest-first, joined with their raw item fields.
    source filters on the raw item's source; interest_id on matched interests
    (checked in Python — keeps the SQL portable across SQLite builds)."""
    sql = (
        "SELECT d.id, d.raw_item_id, d.title, d.summary, d.matched_interests, "
        "d.score, d.created_at, r.source, r.author, r.posted_at, "
        "r.posted_at_approx, r.ingested_at "
        "FROM digest_items d JOIN raw_items r ON r.id = d.raw_item_id "
        "ORDER BY (r.posted_at_approx = '') ASC, r.posted_at_approx DESC, "
        "d.id DESC"
    )
    items = [_digest_to_dict(r) for r in conn.execute(sql).fetchall()]
    if source:
        items = [i for i in items if i["source"] == source]
    if interest_id is not None:
        items = [
            i for i in items
            if any(m.get("id") == interest_id for m in i["matched_interests"])
        ]
    return items


def get_digest_item(conn: sqlite3.Connection, item_id: int) -> Optional[Dict[str, Any]]:
    """Full detail: digest fields + raw original (text/media/thread)."""
    row = conn.execute(
        "SELECT d.*, r.source, r.author, r.posted_at, r.text, r.media_desc, "
        "r.thread_json, r.ingested_at, r.sponsored "
        "FROM digest_items d JOIN raw_items r ON r.id = d.raw_item_id "
        "WHERE d.id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    d = _digest_to_dict(row)
    d["thread"] = json.loads(d.pop("thread_json") or "[]")
    return d


# ---------------------------------------------------------------------------
# threads & thread_messages (item chat, §5)
# ---------------------------------------------------------------------------

def get_or_create_thread(conn: sqlite3.Connection, digest_item_id: int) -> int:
    """One conversation thread per digest item (threads.digest_item_id UNIQUE)."""
    row = conn.execute(
        "SELECT id FROM threads WHERE digest_item_id = ?", (digest_item_id,)
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO threads (digest_item_id) VALUES (?)", (digest_item_id,)
    )
    conn.commit()
    return cur.lastrowid


def add_thread_message(conn: sqlite3.Connection, thread_id: int,
                       role: str, content: str) -> int:
    cur = conn.execute(
        "INSERT INTO thread_messages (thread_id, role, content) VALUES (?, ?, ?)",
        (thread_id, role, content),
    )
    conn.commit()
    return cur.lastrowid


def list_thread_messages(conn: sqlite3.Connection, thread_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, role, content, created_at FROM thread_messages "
        "WHERE thread_id = ? ORDER BY id",
        (thread_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# drafts (§6)
# ---------------------------------------------------------------------------

def insert_draft(conn: sqlite3.Connection, digest_item_id: int,
                 channel: str, content: str) -> int:
    cur = conn.execute(
        "INSERT INTO drafts (digest_item_id, channel, content) VALUES (?, ?, ?)",
        (digest_item_id, channel, content),
    )
    conn.commit()
    return cur.lastrowid


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return dict(row) if row is not None else None


def list_drafts(conn: sqlite3.Connection, digest_item_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM drafts WHERE digest_item_id = ? ORDER BY id DESC",
        (digest_item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_draft_content(conn: sqlite3.Connection, draft_id: int, content: str) -> bool:
    """Persist user edits before sending (drafts must be user-confirmed)."""
    cur = conn.execute(
        "UPDATE drafts SET content = ? WHERE id = ? AND status = 'draft'",
        (content, draft_id),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_draft_sent(conn: sqlite3.Connection, draft_id: int) -> bool:
    cur = conn.execute(
        "UPDATE drafts SET status = 'sent', sent_at = datetime('now') "
        "WHERE id = ? AND status = 'draft'",
        (draft_id,),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# meta (collection status for the UI, §8 GET /api/digest/status)
# ---------------------------------------------------------------------------

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def save_collection_status(conn: sqlite3.Connection, detail: Dict[str, Any]) -> None:
    """Record the outcome of the last collection round (JSON in meta)."""
    set_meta(conn, "last_collect", json.dumps(detail, ensure_ascii=False))


def get_collection_status(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    raw = get_meta(conn, "last_collect")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
