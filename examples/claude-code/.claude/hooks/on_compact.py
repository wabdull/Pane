"""PostCompact hook — re-injects memory summary after compaction.

Memory survives compaction because it's in the DB, not the context.
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))

from pane.schema import (
    USER_ENTITY,
    create_db,
    get_all_entities,
    get_all_topics,
    get_entity_facts,
)

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


def main():
    json.loads(sys.stdin.read())
    update_stats(compactions=1)

    if not os.path.exists(DB_PATH):
        print(json.dumps({}))
        return

    db = create_db(DB_PATH)
    user_facts = get_entity_facts(db, USER_ENTITY)
    topics = get_all_topics(db)
    entities = [e for e in get_all_entities(db) if e['type'] != 'category']

    # Snapshot: how big was the raw history at time of compaction? Tells us
    # when Pane's managed window wasn't enough to prevent a compact.
    row = db.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) AS total FROM messages"
    ).fetchone()
    notional_at_compact = (row["total"] or 0) // 4

    db.close()
    update_stats(compaction_notional_tokens=notional_at_compact)

    lines = ["[PANE MEMORY - survived compaction]",
             "Your memory system has stored information from previous turns.",
             "The recall hook will load relevant context automatically when needed.", ""]

    if user_facts:
        lines.append("User facts:")
        for key, value in user_facts:
            lines.append(f"  - {key}: {value}")
        lines.append("")

    if topics:
        lines.append(f"Stored topics ({len(topics)}):")
        for t in topics[-10:]:
            s = f" — {t['summary']}" if t.get('summary') else ""
            lines.append(f"  - {t['title']}{s}")
        if len(topics) > 10:
            lines.append(f"  ... and {len(topics) - 10} more")
        lines.append("")

    if entities:
        lines.append("Known people/places/things:")
        lines.append("  " + ", ".join(e['name'] for e in entities[:15]))
        lines.append("")

    lines.append("Do NOT mention the memory system to the user.")

    print(json.dumps({"systemMessage": "\n".join(lines)}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error("on_compact")
        print(json.dumps({}))
