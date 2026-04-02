"""PostCompact hook — re-injects memory summary after compaction.

Memory survives compaction because it's in the DB, not the context.
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))

from pane.schema import create_db, get_all_topics, get_facts, get_all_entities

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'pane.db')
STATS_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'stats.json')


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
    facts = get_facts(db)
    topics = get_all_topics(db)
    entities = [e for e in get_all_entities(db) if e['type'] not in ('fact', 'category')]
    db.close()

    lines = ["[PANE MEMORY — survived compaction]",
             "Your memory system has stored information from previous turns.",
             "The recall hook will load relevant context automatically when needed.", ""]

    if facts:
        lines.append("Known facts:")
        for name, value in facts.items():
            lines.append(f"  - {name}: {value}")
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
        print(json.dumps({}))
