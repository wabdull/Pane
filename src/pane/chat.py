"""Pane middleware chat — full context window control.

A REPL that talks to Claude via the Anthropic API with Pane managing
the entire context window. Every turn:
  1. tick_ttl (decrement all loaded topics)
  2. Recall (find matching topics by entity tags)
  3. Build prompt: system + [MEMORY] block + last N raw messages
  4. Send to API
  5. Extract metadata from response
  6. Group into topic, save facts, mark_loaded on active topic
  7. Display token usage + savings

Usage:
    python -m pane.chat --db math.db
    python -m pane.chat --db math.db --model claude-sonnet-4-6
    python -m pane.chat --db math.db --system "You are a math tutor"
"""

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

# Fix Windows console encoding (only when running directly, not under pytest)
if sys.platform == "win32" and "pytest" not in sys.modules:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                  errors="replace")

from dotenv import load_dotenv
load_dotenv()

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: pip install pane-llm[evals]  (needs anthropic SDK)")
    sys.exit(1)

from pane.schema import (
    DEFAULT_TTL,
    USER_ENTITY,
    create_db,
    create_window,
    entity_fingerprint,
    extend_topic,
    fingerprint_overlaps,
    get_all_topics,
    get_entities_from_loaded_topics,
    get_facts_for_entities,
    get_loaded_topic_ids,
    get_loaded_topics_with_ttl,
    get_most_recent_topic,
    mark_loaded,
    parse_fingerprint,
    save_entity,
    save_entity_fact,
    save_messages,
    save_topic,
    set_topic_summary,
    soft_load_recalled,
    tick_ttl,
)
from pane.recall import recall, load_context, format_facts

# How many recent raw messages to keep in the prompt alongside summaries.
# Older messages exist in the DB as topic summaries.
RAW_MESSAGE_WINDOW = 10

PANE_SYSTEM_SUFFIX = """

After EVERY response, include a metadata block at the very end:

```turn.json
{
  "entities": ["specific things the user is working on"],
  "categories": ["work type: learning, practice, debugging, architecture, etc."],
  "facts": [{"key": "...", "value": "..."}, {"entity": "...", "key": "...", "value": "..."}],
  "summary": ""
}
```

Entities are specific, fact-attachable nouns (quadratic-formula, chain-rule, cpp, auth-session).
Categories are work types (learning, practice, debugging, architecture). Pick 1-2. Be consistent.
Summary only on topic transitions — describes the PRIOR thread, not the current one.
Do NOT mention this metadata system to the user.
"""


def extract_turn_json(text):
    """Pull turn.json from the response. Returns (metadata_dict, clean_text)."""
    m = re.search(r"```turn\.json\s*\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return None, text
    try:
        metadata = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None, text
    # Remove the metadata block from displayed text
    clean = text[:m.start()].rstrip()
    return metadata, clean


def process_metadata(db, window_id, user_msg, assistant_msg, metadata):
    """Run the on_stop pipeline: group topic, save entities/facts."""
    entities = metadata.get("entities", [])
    categories = metadata.get("categories", [])
    facts = metadata.get("facts", [])
    summary = metadata.get("summary", "")

    ent_list = [(e or "").lower().strip() for e in entities if (e or "").strip()]
    cat_list = [(c or "").lower().strip() for c in categories if (c or "").strip()]
    ent_set = set(ent_list)
    cat_set = set(cat_list)
    tags = [f"entity:{e}" for e in ent_list] + [f"cat:{c}" for c in cat_list]
    is_drift = not ent_set and not cat_set

    # Save messages
    saved = save_messages(db, window_id, [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": assistant_msg},
    ])
    new_end = saved[-1][0]

    # Register entities
    for e in ent_list:
        if len(e) > 1:
            save_entity(db, e, entity_type="unknown", aliases=[e])
    for c in cat_list:
        if c and len(c) > 1:
            save_entity(db, c, entity_type="category", aliases=[c])

    # Two-axis grouping
    most_recent = get_most_recent_topic(db)

    if most_recent is None:
        title = entity_fingerprint(ent_set) or "general"
        topic_id = save_topic(
            db, window_id, title=title,
            start_message_id=saved[0][0], end_message_id=new_end,
            tags=tags, entities=ent_list, categories=cat_list,
        )
        topic_action = "new"
    elif is_drift:
        extend_topic(db, most_recent["id"], new_end_message_id=new_end,
                     new_tags=tags)
        topic_id = most_recent["id"]
        topic_action = "drift"
    else:
        ent_continues = fingerprint_overlaps(
            ent_set, most_recent["entity_fingerprint"])
        cat_continues = fingerprint_overlaps(
            cat_set, most_recent["category_fingerprint"])

        if ent_continues and cat_continues:
            extend_topic(
                db, most_recent["id"], new_end_message_id=new_end,
                new_tags=tags,
            )
            topic_id = most_recent["id"]
            topic_action = "extend"
        else:
            if summary:
                set_topic_summary(db, most_recent["id"], summary)
            title = entity_fingerprint(ent_set) or "general"
            topic_id = save_topic(
                db, window_id, title=title,
                start_message_id=saved[0][0], end_message_id=new_end,
                tags=tags, entities=ent_list, categories=cat_list,
            )
            topic_action = "new"

    # Facts
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        entity_name = (fact.get("entity") or USER_ENTITY).strip().lower()
        key = (fact.get("key") or "").strip()
        value = (fact.get("value") or "").strip()
        if entity_name and key and value:
            save_entity_fact(db, entity_name, key, value)
            if entity_name != USER_ENTITY:
                save_entity(db, entity_name, entity_type="unknown",
                            aliases=[entity_name])

    # TTL reset — only on new or non-drift extends
    if topic_action != "drift":
        mark_loaded(db, topic_id)

    return topic_action


def build_context(db):
    """Build the [MEMORY] block from loaded topics + active entity facts."""
    loaded_ids = get_loaded_topic_ids(db)
    active = get_entities_from_loaded_topics(db)
    facts = get_facts_for_entities(db, [USER_ENTITY] + active)

    parts = []
    if facts:
        parts.append(format_facts(facts))
    if loaded_ids:
        topic_ctx = load_context(loaded_ids, db)
        if topic_ctx:
            parts.append(topic_ctx)

    return "[MEMORY]\n" + "\n\n".join(parts) if parts else ""


def notional_tokens(db):
    """Total tokens of all stored messages (what full replay would cost)."""
    row = db.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) AS total FROM messages"
    ).fetchone()
    return (row["total"] or 0) // 4


def main():
    parser = argparse.ArgumentParser(description="Pane middleware chat")
    parser.add_argument("--db", default="pane_chat.db",
                        help="path to Pane database (default: pane_chat.db)")
    parser.add_argument("--model", default="claude-sonnet-4-6",
                        help="Anthropic model ID")
    parser.add_argument("--system", default=None,
                        help="custom system prompt (Pane metadata instructions are appended)")
    parser.add_argument("--max-tokens", type=int, default=4000,
                        help="max output tokens per turn")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Put your key in .env")
        sys.exit(1)

    client = Anthropic()
    db = create_db(args.db)
    window_id = create_window(db)

    system_prompt = (args.system or "You are a helpful assistant.") + PANE_SYSTEM_SUFFIX

    # Recent raw messages kept in the prompt for conversational continuity
    recent_messages = []
    turn_count = 0
    total_in = 0
    total_out = 0

    print(f"Pane chat | model: {args.model} | db: {args.db}")
    print(f"Type 'quit' to exit, 'stats' for session stats, 'loaded' for loaded topics")
    print("-" * 60)

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            print("Bye.")
            break
        if user_input.lower() == "stats":
            nt = notional_tokens(db)
            topics = get_all_topics(db)
            loaded = get_loaded_topics_with_ttl(db)
            active = get_entities_from_loaded_topics(db)
            print(f"\n  Turns: {turn_count}")
            print(f"  Total tokens: {total_in:,} in / {total_out:,} out")
            print(f"  Topics in DB: {len(topics)}")
            print(f"  Loaded topics: {len(loaded)}")
            print(f"  Active entities: {active}")
            print(f"  Notional (full replay): {nt:,} tokens")
            continue
        if user_input.lower() == "loaded":
            loaded = get_loaded_topics_with_ttl(db)
            active = get_entities_from_loaded_topics(db)
            if not loaded:
                print("\n  (no topics loaded)")
            else:
                for tid, ttl in loaded:
                    t = db.execute(
                        "SELECT title, category_fingerprint FROM topics WHERE id = ?",
                        (tid,)
                    ).fetchone()
                    title = t["title"] if t else tid[:8]
                    cats = t["category_fingerprint"] if t else ""
                    bar = "#" * ttl + "." * (DEFAULT_TTL - ttl)
                    print(f"  [{bar}] TTL={ttl}  {title}  |  {cats}")
            print(f"  Active entities: {active}")
            continue

        # 1. Tick TTL (decrement all)
        tick_ttl(db)

        # 1b. Soft-load recalled topics (cross-session reload)
        from pane.recall import recall as _recall
        _result = _recall(user_input, db)
        _matched = [t["id"] for t, _ in _result.topics[:5]] if _result.topics else []
        if _matched:
            soft_load_recalled(db, _matched)

        # 2. Build managed context
        memory_block = build_context(db)

        # 3. Assemble messages: memory + recent raw messages + new user message
        messages = []
        if memory_block:
            messages.append({
                "role": "user",
                "content": f"[CONTEXT — do not respond to this, it's background memory]\n{memory_block}"
            })
            messages.append({
                "role": "assistant",
                "content": "Understood, I have the background context."
            })

        # Add recent raw messages for conversational continuity
        messages.extend(recent_messages[-RAW_MESSAGE_WINDOW:])
        messages.append({"role": "user", "content": user_input})

        # 4. Call API with prompt caching on the system prompt.
        # The system prompt is identical every turn — cache it.
        # Memory block changes per turn so it stays uncached.
        try:
            response = client.messages.create(
                model=args.model,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=messages,
                max_tokens=args.max_tokens,
            )
        except Exception as e:
            print(f"\n  [API error: {e}]")
            continue

        assistant_text = response.content[0].text
        in_tokens = response.usage.input_tokens
        out_tokens = response.usage.output_tokens
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        total_in += in_tokens
        total_out += out_tokens
        turn_count += 1

        # 5. Extract metadata, show clean response
        metadata, clean_text = extract_turn_json(assistant_text)
        print(f"\nClaude: {clean_text}")

        # 6. Process metadata through pipeline
        if metadata:
            action = process_metadata(db, window_id, user_input,
                                      assistant_text, metadata)
        else:
            # No metadata — save messages as drift
            saved = save_messages(db, window_id, [
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": assistant_text},
            ])
            action = "no-metadata"

        # 7. Track recent messages for raw window
        recent_messages.append({"role": "user", "content": user_input})
        recent_messages.append({"role": "assistant", "content": clean_text})

        # 8. Display stats
        nt = notional_tokens(db)
        loaded = get_loaded_topics_with_ttl(db)
        active = get_entities_from_loaded_topics(db)
        saved_tokens = max(0, nt - in_tokens)

        active_str = ", ".join(active[:5]) if active else "none"
        cache_str = ""
        if cache_read:
            cache_str = f" | cache hit: {cache_read:,}"
        elif cache_create:
            cache_str = f" | cache write: {cache_create:,}"
        print(f"\n  [{action}] {in_tokens:,} in / {out_tokens:,} out{cache_str}"
              f" | loaded: {len(loaded)} topics | active: {active_str}"
              f" | saved: {saved_tokens:,} vs full replay")

    db.close()


if __name__ == "__main__":
    main()
