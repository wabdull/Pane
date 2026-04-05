# Pane

Context window manager for LLMs. Memory for free.

## The Problem

LLMs forget everything between sessions. Context windows grow until they're expensive and the model's recall degrades. Compaction (summarizing old context) costs money and loses detail. Every existing memory system adds LLM calls for retrieval, extraction, or both.

## What Pane Does

Pane sits between the user and any LLM. It manages what's in the context window — loading relevant history when needed, unloading stale topics when they're not. Nothing is ever lost. Nothing is ever summarized away.

- **Topics load on demand.** Ask about something from 3 months ago? Pane finds it and loads it. Not talking about it anymore? It unloads after N turns. Context stays lean.
- **Facts are captured automatically.** "My commute is 35 minutes" — stored instantly, recallable forever. The user never has to say "save this."
- **Summaries replace raw messages.** A 15,000-token conversation about pricing becomes a 50-token summary: "3 tiers: Free, Pro $7.99/mo, Team $14.99/mo." Full detail stays in the DB for when you need it.
- **Memory survives compaction.** When the context window compacts, Pane re-injects what matters. The compaction can't lose what's in the database.
- **Zero retrieval cost.** No embeddings. No LLM calls for search. Tag intersection on entity + category tags — the same tags the LLM already emitted while responding.

## How It Works

```
User sends message
    │
    ▼
[Hook] Search memory DB for relevant topics/facts
    │
    ▼
Inject matching context into the LLM's prompt
    │
    ▼
LLM responds normally + writes metadata (entities, facts, topic)
    │
    ▼
[Hook] Store metadata + messages to DB
    │
    ▼
Stale topics auto-unload from context (TTL countdown)
```

The metadata emission costs ~100-200 extra output tokens per turn. The LLM is already generating a response — the metadata piggybacks on that. Retrieval is tag intersection in SQLite — instant, free.

## What's Always In Context (~500 tokens)

- User identity (name, role)
- Directives (preferences, rules)

Everything else — facts, topics, conversation history — loads on demand and unloads when stale.

## What's Killed

| Approach | Why It's Gone |
|---|---|
| Embeddings | Proven 46% ceiling on retrieval accuracy |
| Keyword extraction | Noisy — stems from code blocks pollute results |
| LLM retrieval calls | Expensive, slow, unreliable |
| Compaction | Lossy AND expensive — just clear and reload from DB |

## What's Kept

| Approach | Why It Works |
|---|---|
| Entity + category tags | Speaker emits them free, high precision |
| Fact store | Instant lookup, no search needed |
| Summaries on resolution | 50 tokens replaces 15K of raw conversation |
| TTL-based topic management | Context stays focused automatically |

## Quick Start (Claude Code)

Copy the `examples/claude-code/` contents into your project:

```
your-project/
  .claude/
    settings.json          ← hook configuration
    hooks/
      on_message.py        ← recall (loads context before LLM sees your message)
      on_stop.py           ← store (saves metadata after LLM responds)
      on_compact.py        ← survive (re-injects memory after compaction)
    memory/                ← runtime data (DB, stats)
  CLAUDE.md                ← tells the LLM to emit metadata
  src/pane/                ← core library (or pip install)
```

Then use Claude Code normally. Memory builds silently.

## Architecture

<img src="docs/architecture.svg" alt="Pane Architecture" width="100%">

## Research

Built through systematic experimentation across multiple retrieval approaches. Key findings:

- **Model quality > retrieval algorithm.** A better LLM reading content beats clever retrieval with a weaker model.
- **Reranking always hurts.** Narrowing candidates then reranking loses the right answer more than it promotes it.
- **Embeddings have a ceiling.** Recurring events embed identically. Cosine similarity can't distinguish instances.
- **The speaker already understands.** Don't extract meaning from text after the fact — capture it while the LLM is generating the response.

## License

MIT
