"""Recall — tag intersection retrieval, zero LLM calls.

Extracts entities from the query and returns:
  - entities: the entities mentioned (used for hard-switch fact loading)
  - topics: matching topics by entity-tag overlap (used for TTL)

No keywords. No embeddings. No LLM calls.
Tags come from what the speaker emitted during conversation.
"""

from pane.types import RecallResult
from pane.schema import (
    USER_ENTITY,
    build_alias_map,
    get_all_topics,
    get_topic_messages,
    get_topics_by_tags,
)


def extract_entities(query, alias_map):
    """Find entity mentions in query using the alias registry.
    Checks longest aliases first to match "dr. chen" before "chen".
    Excludes the user entity (always implicitly present).
    """
    query_lower = query.lower()
    found = set()
    for alias in sorted(alias_map.keys(), key=len, reverse=True):
        if len(alias) > 1 and alias in query_lower:
            canonical = alias_map[alias]
            if canonical != USER_ENTITY:
                found.add(canonical)
    return found


def recall(query, db):
    """Find relevant entities and topics for a user query.
    Returns RecallResult with both.
    """
    alias_map = build_alias_map(db)
    query_entities = extract_entities(query, alias_map)
    topics = _search_topics(query_entities, db)

    mode = "topic" if topics else ("entity" if query_entities else "not_found")
    return RecallResult(
        mode=mode,
        entities=sorted(query_entities),
        topics=topics,
        n_results=len(topics),
    )


def _search_topics(query_entities, db):
    """Tag intersection search. Returns list of (topic_dict, score)."""
    if not query_entities:
        return []

    tags = [f"entity:{e}" for e in query_entities]
    # Also check category tags — entities and categories share a namespace
    tags.extend(f"cat:{e}" for e in query_entities)

    candidates = get_topics_by_tags(db, tags)
    if not candidates:
        return []

    all_topics = {t["id"]: t for t in get_all_topics(db)}
    results = []
    for c in candidates:
        topic = all_topics.get(c["topic_id"])
        if not topic:
            continue
        score = c["match_count"]
        results.append((topic, score))

    results.sort(key=lambda x: -x[1])
    return results


def load_context(topic_ids, db, max_tokens=30000, use_summary=True):
    """Load topics as formatted context.

    Default: loads summaries (compact). Set use_summary=False for raw messages.
    """
    blocks = []
    tokens_used = 0

    for tid in topic_ids:
        topic = db.execute("SELECT title, summary FROM topics WHERE id = ?", (tid,)).fetchone()
        if not topic:
            continue

        if use_summary and topic["summary"]:
            block = f"[{topic['title']}] {topic['summary']}"
        else:
            messages = get_topic_messages(db, tid)
            if not messages:
                continue
            lines = [f"--- {topic['title']} ---"]
            for m in messages:
                role = "[User]" if m["role"] == "user" else "[AI]"
                lines.append(f"{role} {m['content']}")
            block = "\n".join(lines)

        block_tokens = len(block) // 4
        if max_tokens is not None and tokens_used + block_tokens > max_tokens:
            break

        blocks.append(block)
        tokens_used += block_tokens

    return "\n\n".join(blocks)


def format_facts(facts_by_entity):
    """Format a facts dict ({entity: [(k,v), ...]}) as a compact context block."""
    if not facts_by_entity:
        return ""
    lines = []
    for entity_name in sorted(facts_by_entity.keys()):
        lines.append(f"[{entity_name}]")
        for key, value in facts_by_entity[entity_name]:
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)
