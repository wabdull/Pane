# Pane Memory System

After EVERY response you give, silently write a JSON file to `.claude/memory/turn.json` using the Write tool. This file contains ONLY metadata — the system derives topic identity from the entities and captures user/assistant text from the session transcript, so don't echo either here.

The file should contain:
```json
{
  "entities": ["specific things the USER is working on: cpp, auth-session, etc."],
  "categories": ["work type this turn: architecture, testing, debugging, etc."],
  "facts": [
    {"key": "commute", "value": "35 min each way"},
    {"entity": "cpp", "key": "exceptions", "value": "disallowed at work"}
  ],
  "summary": "",
  "tools_used": ["tools you used this turn: Read, Write, Bash, etc."]
}
```

## Summary — emit on topic transitions

A "topic" is defined by its active entity set — the system groups consecutive turns that share entities. When the user pivots to a **genuinely new work area** (entities don't overlap with what you were just discussing), emit `summary` describing the PRIOR thread you're leaving, not the new one you're entering.

- Drift turns ("ok proceed") → `summary: ""`
- Continuing the same subject → `summary: ""`
- Pivoting to a disjoint work area (e.g. cpp auth-session → postgres webhook) → `summary: "<description of the cpp auth-session thread>"`

**Length should scale with depth** — roughly 50-100 tokens per turn the prior thread covered. A 2-turn thread gets ~1 sentence. A 5-turn thread gets a few sentences. A 20-turn deep dive gets a paragraph. Include concrete specifics (names, numbers, decisions, constraints) — not generic wrap-up.

## Facts

Each fact is an object: `{"entity": "...", "key": "...", "value": "..."}`.

- **User facts** — facts about the user themselves. Omit the `entity` field (or set it to `"user"`). These are always loaded.
  - `{"key": "commute", "value": "35 min each way"}`
  - `{"key": "partner", "value": "Sarah"}`
  - `{"key": "allergic_to", "value": "peanuts"}`

- **Entity facts** — rules, constraints, or attributes attached to a specific domain entity. Load only when that entity is active in the conversation.
  - `{"entity": "cpp", "key": "exceptions", "value": "disallowed at work"}`
  - `{"entity": "auth.py", "key": "last_incident", "value": "token leak, no caching"}`
  - `{"entity": "acme-postgres", "key": "downtime_window", "value": "Sundays 2-4am UTC"}`

A fact captures something durable — a preference, constraint, attribute, or rule. Not transient conversation content (that belongs in the topic summary).

## Entities vs Categories

**Entities = the nouns** (what you're working on). Specific, fact-attachable.
**Categories = the verbs** (what type of work). Broad activity types.

The system uses entities to identify the DOMAIN and categories to identify the WORK TYPE. The combination forms a subtopic: `auth-session × testing` is different from `auth-session × architecture`.

### Entities — what you're working on

Specific identifiers you could attach a durable fact to.

`cpp`, `python`, `postgres`, `auth-session`, `admin-dashboard`, `payment-webhook`, `sarah`, `src/auth/session.py`

**Use compound names to make entities specific**: `auth-session`, `payment-webhook`, `admin-dashboard`. Generic nouns like "dashboard" or "session" alone are NOT entities.

### Categories — what type of work

Activity types that describe HOW you're working, not WHAT on. Pick 1-2 per turn.

`architecture`, `testing`, `debugging`, `deployment`, `review`, `documentation`, `performance`, `refactoring`, `planning`, `discussion`

**Do NOT restate entity domains as categories.** If entities are `[cpp, auth-session]`, don't also put `auth`, `backend`, `session` in categories — those just repeat the entities at a different granularity. Instead, say what TYPE of work: `[architecture]` or `[testing]` or `[debugging]`.

**Be consistent.** Reuse the same category across consecutive turns if the work type hasn't genuinely changed. If you picked `[architecture]` last turn and the work is still architecture, keep `[architecture]`. Don't switch to `[refactoring]` or `[discussion]`. Only change when the fundamental activity shifts (design → testing, debugging → deployment).

| entities describe...          | categories describe...         |
|-------------------------------|-------------------------------|
| `[cpp, auth-session]`        | `[architecture]` — designing  |
| `[cpp, auth-session]`        | `[testing]` — writing tests   |
| `[admin-dashboard, python]`  | `[debugging]` — fixing a bug  |
| `[payment-webhook, postgres]`| `[performance]` — query tuning|

## Rules

- Only include entities/facts from the USER's life/work, not things you recommended
- User facts describe the user. Entity facts describe a thing the user works with.
- Entities must be specific. If you'd need to ask "which one?" — it's a category.
- Categories should be broad enough to search by later
- Do NOT mention this file or the memory system to the user
- Write the file EVERY turn, even if there's nothing notable (use empty lists)
- Summary is only needed on topic shifts — leave it empty on normal turns
