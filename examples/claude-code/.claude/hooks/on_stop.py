"""Stop hook — captures metadata from turn.json, stores to DB.

Runs AFTER the LLM finishes responding.
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))

from pane.schema import create_db, create_window, save_messages, save_topic, save_entity

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'pane.db')
METADATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'turn.json')
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
    json.loads(sys.stdin.read())  # consume hook input

    # Read metadata file
    if not os.path.exists(METADATA_PATH):
        print(json.dumps({}))
        return

    try:
        with open(METADATA_PATH, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        os.remove(METADATA_PATH)
    except (json.JSONDecodeError, IOError):
        print(json.dumps({}))
        return

    if not metadata:
        print(json.dumps({}))
        return

    user_msg = metadata.get("user_message", "")
    assistant_msg = metadata.get("assistant_message", "")
    topic = metadata.get("topic", "general")
    summary = metadata.get("summary", "")
    entities = metadata.get("entities", [])
    categories = metadata.get("categories", [])
    facts = metadata.get("facts", [])
    tools = metadata.get("tools_used", [])

    # Store to DB
    db = create_db(DB_PATH)
    db.execute("BEGIN")

    try:
        window_id = create_window(db)

        messages = []
        if user_msg:
            messages.append({"role": "user", "content": user_msg})
        if assistant_msg:
            messages.append({"role": "assistant", "content": assistant_msg})
        if not messages:
            messages.append({"role": "assistant", "content": f"[{topic}]"})

        saved = save_messages(db, window_id, messages)

        # Build tags — entities and categories only, no keywords
        tags = [f"entity:{e.lower().strip()}" for e in entities if e.strip()]
        tags += [f"cat:{c.lower().strip()}" for c in categories if c.strip()]

        save_topic(
            db, window_id,
            title=topic,
            start_message_id=saved[0][0],
            end_message_id=saved[-1][0],
            summary=summary,
            tags=tags,
        )

        # Entity registry
        for ent in entities:
            if ent.strip() and len(ent.strip()) > 1:
                save_entity(db, ent.strip(), entity_type="unknown", aliases=[ent.strip()])
        for cat in categories:
            if cat.strip() and len(cat.strip()) > 1:
                save_entity(db, cat.strip(), entity_type="category", aliases=[cat.strip()])

        # Facts
        for fact in facts:
            if ":" in fact:
                key, value = fact.split(":", 1)
                key, value = key.strip().lower(), value.strip()
                if key and value:
                    save_entity(db, key, entity_type="fact", value=value)

        db.commit()

        tokens_stored = (len(user_msg) + len(assistant_msg)) // 4
        update_stats(
            turns=1,
            tokens_stored=tokens_stored,
            topics_stored=1,
            facts_stored=len([f for f in facts if ":" in f]),
            tool_calls=len(tools),
        )

    except Exception:
        db.rollback()

    db.close()
    print(json.dumps({}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({}))
