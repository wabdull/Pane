"""SQLite schema and CRUD for Pane.

One DB per user. Four tables:
  - messages: raw conversation turns, append-only
  - topics: groups of messages with title, summary, and message range
  - topic_tags: entity/category tags on topics (for retrieval)
  - entities: registry of people, places, tools, facts
"""

import sqlite3
import json
import uuid
from datetime import datetime, timezone


def create_db(path):
    """Initialize a Pane database. Returns connection."""
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS topics (
            id TEXT PRIMARY KEY,
            window_id TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            start_message_id INTEGER NOT NULL,
            end_message_id INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS topic_tags (
            topic_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (topic_id, tag)
        );

        CREATE TABLE IF NOT EXISTS entities (
            name TEXT PRIMARY KEY,
            type TEXT NOT NULL DEFAULT 'unknown',
            aliases TEXT NOT NULL DEFAULT '[]',
            value TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_messages_window ON messages(window_id);
        CREATE INDEX IF NOT EXISTS idx_topics_window ON topics(window_id);
        CREATE INDEX IF NOT EXISTS idx_topic_tag ON topic_tags(tag);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
    """)

    db.commit()
    return db


# ── Messages ──────────────────────────────────────────────────

def save_messages(db, window_id, messages):
    """Insert messages. Returns list of (sqlite_id, content)."""
    saved = []
    for m in messages:
        if hasattr(m, 'content'):
            role, content, ts = m.role, m.content, m.timestamp
        else:
            content = m.get("content", m.get("text", ""))
            role = m.get("role", "user")
            ts = m.get("timestamp", "")

        cursor = db.execute(
            "INSERT INTO messages (window_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (window_id, role, content, ts)
        )
        saved.append((cursor.lastrowid, content))
    return saved


def get_topic_messages(db, topic_id):
    """Get raw messages for a topic by its message range."""
    topic = db.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not topic:
        return []
    rows = db.execute(
        "SELECT * FROM messages WHERE window_id = ? AND id >= ? AND id <= ? ORDER BY id",
        (topic["window_id"], topic["start_message_id"], topic["end_message_id"])
    ).fetchall()
    return [dict(r) for r in rows]


# ── Topics ────────────────────────────────────────────────────

def save_topic(db, window_id, title, start_message_id, end_message_id,
               summary="", tags=None):
    """Store a topic with tags. Returns topic ID."""
    topic_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO topics (id, window_id, title, summary, start_message_id, end_message_id) VALUES (?, ?, ?, ?, ?, ?)",
        (topic_id, window_id, title, summary, start_message_id, end_message_id)
    )
    if tags:
        for tag in tags:
            tag = tag.lower().strip()
            if tag:
                db.execute(
                    "INSERT OR IGNORE INTO topic_tags (topic_id, tag) VALUES (?, ?)",
                    (topic_id, tag)
                )
    return topic_id


def get_all_topics(db):
    """Get all topics, ordered chronologically."""
    rows = db.execute("SELECT * FROM topics ORDER BY start_message_id").fetchall()
    return [dict(r) for r in rows]


def get_topics_by_tags(db, tags):
    """Find topics matching ANY of the given tags. Returns topic IDs with match counts."""
    if not tags:
        return []
    placeholders = ",".join("?" for _ in tags)
    rows = db.execute(
        f"""SELECT topic_id, COUNT(*) as match_count, GROUP_CONCAT(tag) as matched_tags
            FROM topic_tags WHERE tag IN ({placeholders})
            GROUP BY topic_id ORDER BY match_count DESC""",
        tags
    ).fetchall()
    return [dict(r) for r in rows]


# ── Entities ──────────────────────────────────────────────────

def save_entity(db, name, entity_type="unknown", aliases=None, value=""):
    """Create or update an entity. Merges aliases."""
    name_lower = name.lower().strip()
    if not name_lower or len(name_lower) < 2:
        return

    existing = db.execute("SELECT * FROM entities WHERE name = ?", (name_lower,)).fetchone()
    if existing:
        old_aliases = set(json.loads(existing["aliases"]))
        new_aliases = old_aliases | {a.lower().strip() for a in (aliases or [])}
        db.execute(
            "UPDATE entities SET aliases = ?, value = CASE WHEN ? != '' THEN ? ELSE value END WHERE name = ?",
            (json.dumps(sorted(new_aliases)), value, value, name_lower)
        )
    else:
        alias_list = sorted({a.lower().strip() for a in (aliases or [name_lower])})
        db.execute(
            "INSERT INTO entities (name, type, aliases, value) VALUES (?, ?, ?, ?)",
            (name_lower, entity_type, json.dumps(alias_list), value)
        )


def get_facts(db):
    """Get all fact-type entities. Returns dict of name -> value."""
    rows = db.execute("SELECT name, value FROM entities WHERE type = 'fact' AND value != ''").fetchall()
    return {r["name"]: r["value"] for r in rows}


def get_all_entities(db):
    """Get all entities."""
    rows = db.execute("SELECT * FROM entities ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def build_alias_map(db):
    """Build alias -> canonical name lookup."""
    alias_to_name = {}
    rows = db.execute("SELECT name, aliases FROM entities").fetchall()
    for row in rows:
        name = row["name"]
        alias_to_name[name] = name
        for alias in json.loads(row["aliases"]):
            alias_to_name[alias.lower()] = name
    return alias_to_name


# ── Window ────────────────────────────────────────────────────

def create_window(db):
    """Create a new context window. Returns window ID."""
    return str(uuid.uuid4())
