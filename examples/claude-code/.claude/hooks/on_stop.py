"""Stop hook — captures metadata from turn.json, stores to DB.

Runs AFTER the LLM finishes responding.
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))

from pane.schema import (
    USER_ENTITY,
    create_db,
    create_window,
    entity_fingerprint,
    extend_topic,
    get_most_recent_topic,
    mark_loaded,
    parse_fingerprint,
    save_entity,
    save_entity_fact,
    save_messages,
    save_topic,
    set_topic_summary,
)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'pane.db')
METADATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'memory', 'turn.json')
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


def _extract_text(content):
    """Pull text out of a message.content value (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def read_last_turn_from_transcript(transcript_path):
    """Return (user_text, assistant_text) for the most recent turn.

    The transcript is a JSONL file; each line is a message entry with
    `message.role` and `message.content`. We walk backwards to find the
    last assistant message and the user message that prompted it.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return "", ""

    try:
        with open(transcript_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except IOError:
        return "", ""

    user_text = ""
    assistant_text = ""

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = entry.get("message") or {}
        role = msg.get("role")
        text = _extract_text(msg.get("content", ""))

        if role == "assistant" and not assistant_text:
            assistant_text = text
        elif role == "user" and assistant_text and not user_text:
            user_text = text
            break

    return user_text, assistant_text


def main():
    hook_input = json.loads(sys.stdin.read())
    transcript_path = hook_input.get("transcript_path", "")

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

    # Source of truth for user/assistant text is the session transcript
    user_msg, assistant_msg = read_last_turn_from_transcript(transcript_path)
    summary = metadata.get("summary", "")
    entities = metadata.get("entities", [])
    categories = metadata.get("categories", [])
    facts = metadata.get("facts", [])
    tools = metadata.get("tools_used", [])

    # Normalize current-turn entity + category sets
    current_entities = [
        (e or "").lower().strip() for e in entities if (e or "").strip()
    ]
    current_categories = [
        (c or "").lower().strip() for c in categories if (c or "").strip()
    ]
    current_ent_set = set(current_entities)
    current_cat_set = set(current_categories)

    # Store to DB. Python's sqlite3 auto-transactions per statement; each
    # save_* helper commits on its own. Not strictly atomic end-to-end, but
    # turn.json is already consumed so partial writes just mean some fields
    # for this turn are missing — tolerable.
    db = create_db(DB_PATH)
    window_id = create_window(db)

    messages = []
    if user_msg:
        messages.append({"role": "user", "content": user_msg})
    if assistant_msg:
        messages.append({"role": "assistant", "content": assistant_msg})
    if not messages:
        messages.append({"role": "assistant", "content": "[turn]"})

    saved = save_messages(db, window_id, messages)
    new_start, new_end = saved[0][0], saved[-1][0]

    # Build tags — entities and categories only, no keywords
    tags = [f"entity:{e}" for e in current_entities]
    tags += [f"cat:{c}" for c in current_categories]

    # Entity registry (register before topic grouping so alias map is fresh)
    for ent in current_entities:
        if len(ent) > 1:
            save_entity(db, ent, entity_type="unknown", aliases=[ent])
    for cat in categories:
        c = (cat or "").lower().strip()
        if c and len(c) > 1:
            save_entity(db, c, entity_type="category", aliases=[c])

    # ── Topic grouping (two-axis: entity + category) ────────
    #
    # Entity overlap  + category overlap  → EXTEND (same sub-thread)
    # Entity overlap  + category NO overlap → NEW ROW (subtopic shift)
    # No entity overlap                     → NEW ROW (domain shift)
    # Drift (no entities AND no categories) → EXTEND (stays in current topic)
    #
    # On any NEW ROW, the speaker's summary attaches to the PRIOR topic
    # (the one being closed), not the new one.
    most_recent = get_most_recent_topic(db)
    topic_id = None
    topic_action = None
    is_drift = not current_ent_set and not current_cat_set

    if most_recent is None:
        title = entity_fingerprint(current_ent_set) or "general"
        topic_id = save_topic(
            db, window_id, title=title,
            start_message_id=new_start, end_message_id=new_end,
            summary="", tags=tags,
            entities=current_entities, categories=current_categories,
        )
        topic_action = "new"
    elif is_drift:
        # Drift turn — extend whatever we're on
        extend_topic(
            db, most_recent["id"], new_end_message_id=new_end,
            new_tags=tags,
        )
        topic_id = most_recent["id"]
        topic_action = "extend"
    else:
        prior_ent = parse_fingerprint(most_recent["entity_fingerprint"])
        prior_cat = parse_fingerprint(most_recent["category_fingerprint"])

        # Empty axis this turn = "no change" on that axis, not "mismatched."
        # User saying "what pattern?" without naming cpp is NOT a domain switch.
        ent_continues = not current_ent_set or bool(current_ent_set & prior_ent)
        cat_continues = not current_cat_set or bool(current_cat_set & prior_cat)

        if ent_continues and cat_continues:
            # Same sub-thread — extend
            merged_title = entity_fingerprint(prior_ent | current_ent_set) or \
                           most_recent["title"]
            extend_topic(
                db, most_recent["id"], new_end_message_id=new_end,
                new_entities=list(current_ent_set),
                new_categories=list(current_cat_set),
                new_tags=tags, new_title=merged_title,
            )
            topic_id = most_recent["id"]
            topic_action = "extend"
        else:
            # Subtopic shift (entity continues, category changed) or
            # domain shift (entity changed). Either way: close prior, open new.
            if summary:
                set_topic_summary(db, most_recent["id"], summary)
            title = entity_fingerprint(current_ent_set) or "general"
            topic_id = save_topic(
                db, window_id, title=title,
                start_message_id=new_start, end_message_id=new_end,
                summary="", tags=tags,
                entities=current_entities, categories=current_categories,
            )
            topic_action = "new"

    # Entity-attached facts. Supports two shapes in turn.json:
    #   {"entity": "cpp", "key": "exceptions", "value": "disallowed"}
    #   {"key": "commute", "value": "35 min"}  -> attached to 'user' by default
    facts_saved = 0
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        entity_name = (fact.get("entity") or USER_ENTITY).strip().lower()
        key = (fact.get("key") or "").strip()
        value = (fact.get("value") or "").strip()
        if entity_name and key and value:
            save_entity_fact(db, entity_name, key, value)
            facts_saved += 1
            # Make sure the owning entity exists in the registry
            if entity_name != USER_ENTITY:
                save_entity(db, entity_name, entity_type="unknown",
                            aliases=[entity_name])

    # Mark the (new or extended) topic as loaded — refreshes TTL.
    # (All save_* helpers commit internally, no explicit commit needed.)
    mark_loaded(db, topic_id)

    tokens_stored = (len(user_msg) + len(assistant_msg)) // 4
    update_stats(
        turns=1,
        tokens_stored=tokens_stored,
        topics_created=1 if topic_action == "new" else 0,
        topics_extended=1 if topic_action == "extend" else 0,
        facts_stored=facts_saved,
        tool_calls=len(tools),
    )

    db.close()
    print(json.dumps({}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error("on_stop")
        print(json.dumps({}))
