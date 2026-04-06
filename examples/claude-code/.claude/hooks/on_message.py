"""UserPromptSubmit hook — searches memory, injects context.

Runs BEFORE the LLM sees the user's message.

Two parallel tracks:
  1. Topics (conversational history) — TTL-managed, tolerate drift
  2. Entity facts (domain rules) — hard-switch, replace on mention
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))

from pane.schema import (
    USER_ENTITY,
    create_db,
    get_entities_from_loaded_topics,
    get_facts_for_entities,
    get_loaded_topic_ids,
    get_loaded_topics_with_ttl,
    tick_ttl,
)
from pane.recall import recall, load_context, format_facts

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'pane.db')
STATS_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'stats.json')
LOG_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'pane.log')


def log_error(hook_name):
    import traceback
    import datetime
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f"\n{datetime.datetime.now().isoformat()} [{hook_name}]\n")
            f.write(traceback.format_exc())
    except Exception:
        pass


def update_stats(**kwargs):
    try:
        with open(STATS_PATH, 'r') as f:
            stats = json.load(f)
    except (IOError, json.JSONDecodeError):
        stats = {}
    for k, v in kwargs.items():
        stats[k] = stats.get(k, 0) + v
    with open(STATS_PATH, 'w') as f:
        json.dump(stats, f, indent=2)


def merge_stats(**kwargs):
    """Overwrite specific keys (non-additive)."""
    try:
        with open(STATS_PATH, 'r') as f:
            stats = json.load(f)
    except (IOError, json.JSONDecodeError):
        stats = {}
    stats.update(kwargs)
    with open(STATS_PATH, 'w') as f:
        json.dump(stats, f, indent=2)


def main():
    hook_input = json.loads(sys.stdin.read())
    prompt = hook_input.get("prompt", "")

    if not prompt or not os.path.exists(DB_PATH):
        print(json.dumps({}))
        return

    db = create_db(DB_PATH)
    result = recall(prompt, db)

    # ── Topics: TTL track ────────────────────────────────────
    matched_topic_ids = []
    if result.mode == "topic" and result.topics:
        matched_topic_ids = [t["id"] for t, _score in result.topics[:5]]
    tick_ttl(db, matched_topic_ids)
    loaded_topic_ids = get_loaded_topic_ids(db)

    # ── Entity facts: derived from loaded topics ──────────────
    # Active entities = union of entity_fingerprints across all loaded
    # topics. Facts follow topics. Topics follow TTL. One system.
    #
    # - Topic loads -> its entities' facts auto-load
    # - Topic unloads (TTL=0) -> if no other loaded topic has that
    #   entity -> entity's facts drop
    # - Subtopics with the same entity set keep facts alive independently
    active = get_entities_from_loaded_topics(db)

    # User-entity facts always load (identity tier).
    # FUTURE: split user facts into identity vs domain tiers when
    # user fact count exceeds ~100 entries. See docs/ARCHITECTURE.md.
    facts = get_facts_for_entities(db, [USER_ENTITY] + active)

    # ── Build context block ─────────────────────────────────
    parts = []
    if facts:
        parts.append(format_facts(facts))
    if loaded_topic_ids:
        raw = load_context(loaded_topic_ids, db)
        if raw:
            parts.append(raw)

    context = "[MEMORY]\n" + "\n\n".join(parts) if parts else ""

    loaded_with_ttl = get_loaded_topics_with_ttl(db)

    # Notional = what the full raw history would cost if replayed every turn.
    # Actual = what Pane actually injects via summaries + facts.
    # The delta is what Pane is saving you on this turn.
    row = db.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) AS total FROM messages"
    ).fetchone()
    notional_tokens = (row["total"] or 0) // 4

    db.close()

    if context:
        tokens = len(context) // 4
        saved = max(0, notional_tokens - tokens)
        update_stats(
            recalls=1,
            tokens_injected=tokens,
            tokens_saved=saved,
        )
        merge_stats(
            loaded_topics=[{"id": tid, "ttl": ttl} for tid, ttl in loaded_with_ttl],
            active_entities=active,
            last_turn_notional_tokens=notional_tokens,
            last_turn_injected_tokens=tokens,
            last_turn_saved_tokens=saved,
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error("on_message")
        print(json.dumps({}))
