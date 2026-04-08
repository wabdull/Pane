"""SQLite schema and CRUD for Pane.

One DB per user. Seven tables:
  - messages: raw conversation turns, append-only
  - topics: groups of messages with title, summary, and message range
  - topic_tags: entity/category tags on topics (for retrieval)
  - entities: registry of people, places, tools, categories
  - entity_facts: key/value facts attached to an entity
  - loaded_topics: ephemeral TTL state — which topics are in the window
  - active_entities: ephemeral hard-switch set — whose facts are loaded right now
"""

import os
import sqlite3
import json
import uuid

DEFAULT_TTL = int(os.environ.get("PANE_DEFAULT_TTL", "3"))
USER_ENTITY = "user"  # facts attached here are always loaded


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
            entity_fingerprint TEXT NOT NULL DEFAULT '',
            category_fingerprint TEXT NOT NULL DEFAULT '',
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
            aliases TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS entity_facts (
            entity_name TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (entity_name, key)
        );

        CREATE TABLE IF NOT EXISTS loaded_topics (
            topic_id TEXT PRIMARY KEY,
            ttl INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS active_entities (
            entity_name TEXT PRIMARY KEY
        );

        CREATE INDEX IF NOT EXISTS idx_messages_window ON messages(window_id);
        CREATE INDEX IF NOT EXISTS idx_topics_window ON topics(window_id);
        CREATE INDEX IF NOT EXISTS idx_topic_tag ON topic_tags(tag);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
        CREATE INDEX IF NOT EXISTS idx_entity_facts_name ON entity_facts(entity_name);
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

def entity_fingerprint(entities):
    """Canonical fingerprint: sorted comma-joined entity names. Empty if none."""
    normed = {(e or "").lower().strip() for e in (entities or [])}
    normed.discard("")
    return ",".join(sorted(normed))


def parse_fingerprint(fp):
    """Inverse of entity_fingerprint: string -> set of entity names."""
    return {x for x in (fp or "").split(",") if x}


OVERLAP_THRESHOLD = 0.5  # majority of current entities must match


def fingerprint_overlaps(current_set, prior_fingerprint_str):
    """Check if current entities overlap enough with a topic's fixed fingerprint.
    Returns True if >= 50% of current entities exist in the prior fingerprint.
    Empty current set = drift (always returns True — no change on this axis).
    """
    if not current_set:
        return True  # drift = no change
    prior = parse_fingerprint(prior_fingerprint_str)
    if not prior:
        return False
    overlap = len(current_set & prior) / len(current_set)
    return overlap >= OVERLAP_THRESHOLD


def save_topic(db, window_id, title, start_message_id, end_message_id,
               summary="", tags=None, entities=None, categories=None):
    """Store a topic with tags + entity/category fingerprints. Returns topic ID."""
    topic_id = str(uuid.uuid4())
    ent_fp = entity_fingerprint(entities or [])
    cat_fp = entity_fingerprint(categories or [])  # same normalization logic
    db.execute(
        "INSERT INTO topics (id, window_id, title, summary, entity_fingerprint, "
        "category_fingerprint, start_message_id, end_message_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (topic_id, window_id, title, summary, ent_fp, cat_fp,
         start_message_id, end_message_id)
    )
    if tags:
        for tag in tags:
            tag = tag.lower().strip()
            if tag:
                db.execute(
                    "INSERT OR IGNORE INTO topic_tags (topic_id, tag) VALUES (?, ?)",
                    (topic_id, tag)
                )
    db.commit()
    return topic_id


def get_all_topics(db):
    """Get all topics, ordered chronologically."""
    rows = db.execute("SELECT * FROM topics ORDER BY start_message_id").fetchall()
    return [dict(r) for r in rows]


def get_most_recent_topic(db):
    """Return the most-recently-ended topic, or None if the table is empty."""
    row = db.execute(
        "SELECT * FROM topics ORDER BY end_message_id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def extend_topic(db, topic_id, new_end_message_id, new_entities=None,
                 new_categories=None, new_tags=None, new_title=None):
    """Extend an existing topic: push end_message_id forward, merge tags.

    Fingerprints (entity + category) are FIXED at creation — they represent
    the topic's identity, not cumulative content. New entities/categories
    go into topic_tags for retrieval but don't change the fingerprint.
    """
    current = db.execute(
        "SELECT entity_fingerprint, category_fingerprint FROM topics WHERE id = ?",
        (topic_id,)
    ).fetchone()
    if not current:
        return

    # Fingerprints stay fixed — only update end_message_id and optionally title
    if new_title is not None:
        db.execute(
            "UPDATE topics SET end_message_id = ?, title = ? WHERE id = ?",
            (new_end_message_id, new_title, topic_id)
        )
    else:
        db.execute(
            "UPDATE topics SET end_message_id = ? WHERE id = ?",
            (new_end_message_id, topic_id)
        )

    for tag in (new_tags or []):
        t = (tag or "").lower().strip()
        if t:
            db.execute(
                "INSERT OR IGNORE INTO topic_tags (topic_id, tag) VALUES (?, ?)",
                (topic_id, t)
            )
    db.commit()


def set_topic_summary(db, topic_id, summary):
    """Write a summary on an existing topic (used when closing a topic)."""
    db.execute(
        "UPDATE topics SET summary = ? WHERE id = ?",
        ((summary or "").strip(), topic_id)
    )
    db.commit()


def get_topics_by_tags(db, tags):
    """Find topics matching ANY of the given tags. Returns topic IDs with
    match counts, ranked by overlap-count DESC then recency DESC.
    """
    if not tags:
        return []
    placeholders = ",".join("?" for _ in tags)
    rows = db.execute(
        f"""SELECT tt.topic_id,
                   COUNT(*) as match_count,
                   GROUP_CONCAT(tt.tag) as matched_tags,
                   MAX(t.end_message_id) as recency
            FROM topic_tags tt
            JOIN topics t ON t.id = tt.topic_id
            WHERE tt.tag IN ({placeholders})
            GROUP BY tt.topic_id
            ORDER BY match_count DESC, recency DESC""",
        tags
    ).fetchall()
    return [dict(r) for r in rows]


# ── Entities ──────────────────────────────────────────────────

def save_entity(db, name, entity_type="unknown", aliases=None):
    """Create or update an entity. Merges aliases."""
    name_lower = name.lower().strip()
    if not name_lower or len(name_lower) < 2:
        return

    existing = db.execute("SELECT * FROM entities WHERE name = ?", (name_lower,)).fetchone()
    if existing:
        old_aliases = set(json.loads(existing["aliases"]))
        new_aliases = old_aliases | {a.lower().strip() for a in (aliases or [])}
        db.execute(
            "UPDATE entities SET aliases = ? WHERE name = ?",
            (json.dumps(sorted(new_aliases)), name_lower)
        )
    else:
        alias_list = sorted({a.lower().strip() for a in (aliases or [name_lower])})
        db.execute(
            "INSERT INTO entities (name, type, aliases) VALUES (?, ?, ?)",
            (name_lower, entity_type, json.dumps(alias_list))
        )


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


# ── TTL / Loaded Topics ───────────────────────────────────────

def tick_ttl(db):
    """Advance the loaded-topics clock by one turn.

    Decrements ALL loaded topics by 1 and removes any at TTL <= 0.
    Does NOT reset any topics — resets come from on_stop's mark_loaded
    after grouping identifies the specific active topic. This allows
    subtopics with shared entity tags to decay independently.

    Returns the list of topic_ids still loaded, highest TTL first.
    """
    db.execute("UPDATE loaded_topics SET ttl = ttl - 1")
    db.execute("DELETE FROM loaded_topics WHERE ttl <= 0")
    db.commit()
    return get_loaded_topic_ids(db)


def mark_loaded(db, topic_id, max_ttl=DEFAULT_TTL):
    """Explicitly load a topic into the context window (TTL = max_ttl)."""
    db.execute(
        "INSERT INTO loaded_topics (topic_id, ttl) VALUES (?, ?) "
        "ON CONFLICT(topic_id) DO UPDATE SET ttl = excluded.ttl",
        (topic_id, max_ttl)
    )
    db.commit()


def get_loaded_topic_ids(db):
    """Return currently-loaded topic IDs, highest TTL first."""
    rows = db.execute(
        "SELECT topic_id FROM loaded_topics ORDER BY ttl DESC"
    ).fetchall()
    return [r["topic_id"] for r in rows]


def get_entities_from_loaded_topics(db):
    """Derive active entities from what's loaded: union of entity_fingerprints
    across all currently-loaded topics. No separate active_entities table needed.
    """
    rows = db.execute(
        """SELECT DISTINCT t.entity_fingerprint
           FROM loaded_topics lt
           JOIN topics t ON t.id = lt.topic_id"""
    ).fetchall()
    active = set()
    for r in rows:
        active |= parse_fingerprint(r["entity_fingerprint"])
    active.discard(USER_ENTITY)
    return sorted(active)


def get_loaded_topics_with_ttl(db):
    """Return list of (topic_id, ttl), highest TTL first. For debugging/stats."""
    rows = db.execute(
        "SELECT topic_id, ttl FROM loaded_topics ORDER BY ttl DESC"
    ).fetchall()
    return [(r["topic_id"], r["ttl"]) for r in rows]


# ── Entity Facts ──────────────────────────────────────────────

def save_entity_fact(db, entity_name, key, value):
    """Attach a fact to an entity. Upserts on (entity_name, key)."""
    entity = (entity_name or "").lower().strip()
    k = (key or "").strip()
    v = (value or "").strip()
    if not entity or not k or not v:
        return
    db.execute(
        "INSERT INTO entity_facts (entity_name, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(entity_name, key) DO UPDATE SET value = excluded.value",
        (entity, k, v)
    )
    db.commit()


def get_entity_facts(db, entity_name):
    """Return list of (key, value) facts for a single entity."""
    rows = db.execute(
        "SELECT key, value FROM entity_facts WHERE entity_name = ? ORDER BY key",
        ((entity_name or "").lower().strip(),)
    ).fetchall()
    return [(r["key"], r["value"]) for r in rows]


def get_facts_for_entities(db, entity_names):
    """Return {entity_name: [(key, value), ...]} for the given entities."""
    if not entity_names:
        return {}
    names = [(n or "").lower().strip() for n in entity_names if n]
    names = [n for n in names if n]
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    rows = db.execute(
        f"SELECT entity_name, key, value FROM entity_facts "
        f"WHERE entity_name IN ({placeholders}) ORDER BY entity_name, key",
        names
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["entity_name"], []).append((r["key"], r["value"]))
    return out


# ── Active Entities (hard-switch set) ─────────────────────────

def set_active_entities(db, entity_names):
    """Replace the active entity set. Does nothing if entity_names is empty
    (preserves previous active set — "sticky" behavior).
    The user entity is NEVER included here; its facts are always loaded
    unconditionally.
    """
    names = [(n or "").lower().strip() for n in (entity_names or [])]
    names = [n for n in names if n and n != USER_ENTITY]
    if not names:
        return
    db.execute("DELETE FROM active_entities")
    for name in names:
        db.execute(
            "INSERT OR IGNORE INTO active_entities (entity_name) VALUES (?)",
            (name,)
        )
    db.commit()


def clear_active_entities(db):
    """Drop all active entities (no domain context)."""
    db.execute("DELETE FROM active_entities")
    db.commit()


def get_active_entities(db):
    """Return list of currently-active entity names."""
    rows = db.execute(
        "SELECT entity_name FROM active_entities ORDER BY entity_name"
    ).fetchall()
    return [r["entity_name"] for r in rows]
