# Pane Memory System

After EVERY response you give, silently write a JSON file to `.claude/memory/turn.json` using the Write tool. This file contains ONLY metadata — the system derives topic identity from the entities and captures user/assistant text from the session transcript, so don't echo either here.

The file should contain:
```json
{
  "entities": ["people, places, projects, tools the USER mentioned"],
  "categories": ["broad themes: backend, frontend, auth, dashboard, etc."],
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

The test: **"Could I attach a durable fact to this name without needing more context?"**
- Yes → **entity** (specific, fact-attachable)
- No → **category** (broad theme, retrieval only, no facts)

| entity ✅                              | category ✅           |
|---------------------------------------|----------------------|
| cpp, python, postgres                 | backend, frontend    |
| sarah, alice-chen                     | team, management     |
| admin-dashboard, billing-dashboard    | dashboard            |
| auth-session, oauth-session           | session, auth        |
| payment-webhook, github-webhook       | webhook, integration |
| src/auth/session.py, AuthController   | code, api            |

**Generic common nouns are NOT entities.** "dashboard" alone isn't an entity — *which* dashboard? Use the specific name (`admin-dashboard`, `billing-dashboard`) as the entity, and put the generic noun in categories if useful.

**Use compound names to make entities specific**: `auth-session`, `payment-webhook`, `admin-dashboard`, `acme-postgres`, `src/auth/session.py`.

## Rules

- Only include entities/facts from the USER's life/work, not things you recommended
- User facts describe the user. Entity facts describe a thing the user works with.
- Entities must be specific. If you'd need to ask "which one?" — it's a category.
- Categories should be broad enough to search by later
- Do NOT mention this file or the memory system to the user
- Write the file EVERY turn, even if there's nothing notable (use empty lists)
- Summary is only needed on topic shifts — leave it empty on normal turns
