"""Inspect a Pane database — quick summary of stored state.

Run:
    python scripts/inspect_db.py path/to/.claude/memory/pane.db
    python scripts/inspect_db.py path/to/.claude/memory/pane.db --facts
    python scripts/inspect_db.py path/to/.claude/memory/pane.db --messages
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pane.schema import (
    USER_ENTITY,
    create_db,
    get_all_entities,
    get_all_topics,
    get_entities_from_loaded_topics,
    get_entity_facts,
    get_facts_for_entities,
    get_loaded_topics_with_ttl,
    parse_fingerprint,
)


def main():
    parser = argparse.ArgumentParser(description="Inspect a Pane database")
    parser.add_argument("db_path", help="path to pane.db")
    parser.add_argument("--facts", action="store_true", help="show all entity facts")
    parser.add_argument("--messages", action="store_true", help="show message counts per topic")
    parser.add_argument("--stats", action="store_true", help="show stats.json if present")
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"ERROR: {args.db_path} not found")
        sys.exit(1)

    db = create_db(args.db_path)

    # ── Topics ────────────────────────────────────────────────
    topics = get_all_topics(db)
    loaded = get_loaded_topics_with_ttl(db)
    loaded_ids = {tid for tid, _ in loaded}
    active = get_entities_from_loaded_topics(db)

    print(f"Topics: {len(topics)} rows")
    for t in topics:
        ent_fp = t.get("entity_fingerprint", "")
        cat_fp = t.get("category_fingerprint", "")
        span = t["end_message_id"] - t["start_message_id"] + 1
        has_sum = "+" if t.get("summary") else " "
        is_loaded = ""
        for tid, ttl in loaded:
            if tid == t["id"]:
                is_loaded = f"  [TTL={ttl}]"
                break
        print(f"  {has_sum} {ent_fp:35s} | {cat_fp:20s} | {span:3d} msgs{is_loaded}")
        if t.get("summary") and args.messages:
            print(f"    summary: {t['summary'][:80]}...")

    # ── Loaded state ──────────────────────────────────────────
    print(f"\nLoaded: {len(loaded)} topics")
    for tid, ttl in loaded:
        t = db.execute("SELECT title FROM topics WHERE id = ?", (tid,)).fetchone()
        print(f"  TTL={ttl}  {t['title'] if t else tid[:8]}")

    print(f"\nActive entities (derived): {active}")

    # ── Entities ──────────────────────────────────────────────
    entities = get_all_entities(db)
    non_cat = [e for e in entities if e["type"] != "category"]
    cats = [e for e in entities if e["type"] == "category"]
    print(f"\nEntities: {len(non_cat)} registered")
    for e in non_cat[:20]:
        aliases = json.loads(e["aliases"])
        alias_str = f"  (aliases: {', '.join(aliases)})" if len(aliases) > 1 else ""
        print(f"  {e['name']}{alias_str}")
    if len(non_cat) > 20:
        print(f"  ... and {len(non_cat) - 20} more")

    print(f"\nCategories: {len(cats)} registered")
    if cats:
        print(f"  {', '.join(e['name'] for e in cats[:30])}")

    # ── Facts ─────────────────────────────────────────────────
    if args.facts:
        print(f"\nEntity facts:")
        all_ent_names = [e["name"] for e in entities] + [USER_ENTITY]
        all_facts = get_facts_for_entities(db, all_ent_names)
        total = 0
        for ent_name in sorted(all_facts.keys()):
            kvs = all_facts[ent_name]
            total += len(kvs)
            print(f"  [{ent_name}]")
            for k, v in kvs:
                print(f"    {k}: {v}")
        print(f"  Total: {total} facts")
    else:
        # Just show counts
        user_facts = get_entity_facts(db, USER_ENTITY)
        row = db.execute("SELECT COUNT(*) as c FROM entity_facts").fetchone()
        print(f"\nFacts: {row['c']} total ({len(user_facts)} user)")

    # ── Messages ──────────────────────────────────────────────
    msg_count = db.execute("SELECT COUNT(*) as c FROM messages").fetchone()
    print(f"\nMessages: {msg_count['c']} stored")

    # ── Stats file ────────────────────────────────────────────
    if args.stats:
        stats_path = Path(args.db_path).parent / "stats.json"
        if stats_path.exists():
            with open(stats_path, encoding="utf-8") as f:
                stats = json.load(f)
            print(f"\nStats:")
            for k, v in sorted(stats.items()):
                if isinstance(v, (list, dict)):
                    print(f"  {k}: {json.dumps(v)[:80]}...")
                else:
                    print(f"  {k}: {v}")
        else:
            print(f"\nNo stats.json at {stats_path}")

    db.close()


if __name__ == "__main__":
    main()
