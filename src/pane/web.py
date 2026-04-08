"""Pane web UI — chat interface with live context panel.

A simple FastAPI server that serves a chat page with:
  - Left: chat messages
  - Right: loaded topics (TTL bars), active entities, token stats

Usage:
    python -m pane.web --db math.db
    python -m pane.web --db math.db --model claude-sonnet-4-6 --port 8080
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

try:
    from anthropic import Anthropic
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
except ImportError as e:
    print(f"ERROR: missing dependency: {e}")
    print("Run: pip install anthropic fastapi uvicorn")
    sys.exit(1)

from pane.schema import (
    DEFAULT_TTL,
    USER_ENTITY,
    create_db,
    create_window,
    get_all_topics,
    get_entities_from_loaded_topics,
    get_facts_for_entities,
    get_loaded_topic_ids,
    get_loaded_topics_with_ttl,
    tick_ttl,
)
from pane.recall import load_context, format_facts
from pane.chat import (
    PANE_SYSTEM_SUFFIX,
    RAW_MESSAGE_WINDOW,
    build_context,
    extract_turn_json,
    notional_tokens,
    process_metadata,
)

# Globals (set in main)
client = None
db = None
window_id = None
model_name = None
system_prompt = None
recent_messages = []
turn_count = 0
total_in = 0
total_out = 0

app = FastAPI()

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pane</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #e6edf3; height: 100vh; display: flex; }

  #sidebar { width: 320px; background: #161b22; border-right: 1px solid #30363d;
             padding: 16px; overflow-y: auto; flex-shrink: 0; }
  #sidebar h2 { font-size: 13px; color: #8b949e; text-transform: uppercase;
                letter-spacing: 1px; margin-bottom: 12px; }
  #sidebar h3 { font-size: 12px; color: #8b949e; margin: 16px 0 8px; }

  .topic-row { background: #21262d; border-radius: 6px; padding: 10px;
               margin-bottom: 8px; font-size: 13px; }
  .topic-title { color: #58a6ff; font-weight: 600; margin-bottom: 4px; }
  .topic-cats { color: #8b949e; font-size: 11px; }
  .ttl-bar { display: flex; gap: 3px; margin-top: 6px; }
  .ttl-block { width: 16px; height: 8px; border-radius: 2px; }
  .ttl-active { background: #3fb950; }
  .ttl-empty { background: #21262d; border: 1px solid #30363d; }

  .entity-tag { display: inline-block; background: #1f6feb33; color: #58a6ff;
                padding: 2px 8px; border-radius: 12px; font-size: 11px;
                margin: 2px 4px 2px 0; }
  .fact-row { font-size: 12px; color: #8b949e; padding: 2px 0; }
  .fact-key { color: #d2a8ff; }

  .stat-row { font-size: 12px; color: #8b949e; padding: 2px 0; }
  .stat-val { color: #e6edf3; font-weight: 600; }

  #main { flex: 1; display: flex; flex-direction: column; }
  #header { padding: 12px 20px; border-bottom: 1px solid #30363d;
            font-size: 14px; color: #8b949e; }
  #header span { color: #58a6ff; }

  #messages { flex: 1; overflow-y: auto; padding: 20px; }
  .msg { max-width: 75%; margin-bottom: 16px; padding: 12px 16px;
         border-radius: 12px; line-height: 1.5; font-size: 14px;
         white-space: pre-wrap; word-wrap: break-word; }
  .msg-user { background: #1f6feb; margin-left: auto; border-bottom-right-radius: 4px; }
  .msg-assistant { background: #21262d; border: 1px solid #30363d;
                   border-bottom-left-radius: 4px; }
  .msg-meta { font-size: 11px; color: #8b949e; margin-top: 6px; }

  #input-area { padding: 16px 20px; border-top: 1px solid #30363d;
                display: flex; gap: 12px; }
  #input-area input { flex: 1; padding: 10px 16px; background: #21262d;
                      border: 1px solid #30363d; border-radius: 8px;
                      color: #e6edf3; font-size: 14px; outline: none; }
  #input-area input:focus { border-color: #58a6ff; }
  #input-area button { padding: 10px 24px; background: #238636; color: white;
                       border: none; border-radius: 8px; cursor: pointer;
                       font-size: 14px; font-weight: 600; }
  #input-area button:hover { background: #2ea043; }
  #input-area button:disabled { background: #21262d; color: #484f58; cursor: not-allowed; }

  .loading { color: #8b949e; font-style: italic; }
</style>
</head>
<body>

<div id="sidebar">
  <h2>Pane Context</h2>

  <h3>Loaded Topics</h3>
  <div id="topics">(none)</div>

  <h3>Active Entities</h3>
  <div id="entities">(none)</div>

  <h3>Facts</h3>
  <div id="facts">(none)</div>

  <h3>Stats</h3>
  <div id="stats"></div>
</div>

<div id="main">
  <div id="header">Pane &mdash; model: <span id="model-name"></span></div>
  <div id="messages"></div>
  <div id="input-area">
    <input id="input" type="text" placeholder="Type a message..." autofocus
           onkeydown="if(event.key==='Enter')sendMessage()">
    <button id="send-btn" onclick="sendMessage()">Send</button>
  </div>
</div>

<script>
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  sendBtn.disabled = true;

  addMessage(text, 'user');
  const loadingEl = addMessage('Thinking...', 'assistant loading');

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
    const data = await res.json();
    loadingEl.remove();
    addMessage(data.response, 'assistant', data.meta);
    updateSidebar(data.sidebar);
  } catch (e) {
    loadingEl.remove();
    addMessage('Error: ' + e.message, 'assistant');
  }
  sendBtn.disabled = false;
  inputEl.focus();
}

function addMessage(text, role, meta) {
  const div = document.createElement('div');
  div.className = 'msg msg-' + role.split(' ')[0];
  if (role.includes('loading')) div.className += ' loading';
  div.textContent = text;
  if (meta) {
    const metaDiv = document.createElement('div');
    metaDiv.className = 'msg-meta';
    metaDiv.textContent = meta;
    div.appendChild(metaDiv);
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function updateSidebar(sb) {
  // Topics
  const topicsEl = document.getElementById('topics');
  if (sb.topics.length === 0) {
    topicsEl.innerHTML = '<div style="color:#484f58;font-size:12px">(none)</div>';
  } else {
    topicsEl.innerHTML = sb.topics.map(t => {
      const blocks = Array.from({length: """ + str(DEFAULT_TTL) + """}, (_, i) =>
        `<div class="ttl-block ${i < t.ttl ? 'ttl-active' : 'ttl-empty'}"></div>`
      ).join('');
      return `<div class="topic-row">
        <div class="topic-title">${t.title}</div>
        <div class="topic-cats">${t.categories || '(none)'}</div>
        <div class="ttl-bar">${blocks}</div>
      </div>`;
    }).join('');
  }

  // Entities
  const entsEl = document.getElementById('entities');
  if (sb.active_entities.length === 0) {
    entsEl.innerHTML = '<div style="color:#484f58;font-size:12px">(none)</div>';
  } else {
    entsEl.innerHTML = sb.active_entities.map(e =>
      `<span class="entity-tag">${e}</span>`
    ).join('');
  }

  // Facts
  const factsEl = document.getElementById('facts');
  if (Object.keys(sb.facts).length === 0) {
    factsEl.innerHTML = '<div style="color:#484f58;font-size:12px">(none)</div>';
  } else {
    factsEl.innerHTML = Object.entries(sb.facts).map(([ent, kvs]) =>
      `<div style="margin-bottom:8px"><strong style="color:#d2a8ff">[${ent}]</strong>` +
      kvs.map(([k,v]) => `<div class="fact-row"><span class="fact-key">${k}:</span> ${v}</div>`).join('') +
      `</div>`
    ).join('');
  }

  // Stats
  document.getElementById('stats').innerHTML = `
    <div class="stat-row">Turn: <span class="stat-val">${sb.turn}</span></div>
    <div class="stat-row">Tokens in: <span class="stat-val">${sb.tokens_in.toLocaleString()}</span></div>
    <div class="stat-row">Tokens out: <span class="stat-val">${sb.tokens_out.toLocaleString()}</span></div>
    <div class="stat-row">Cache: <span class="stat-val">${sb.cache}</span></div>
    <div class="stat-row" style="margin-top:8px;padding-top:8px;border-top:1px solid #30363d">
      <strong style="color:#58a6ff">Context Window</strong></div>
    <div class="stat-row">With Pane: <span class="stat-val">${sb.pane_tokens.toLocaleString()} tokens</span></div>
    <div class="stat-row">Without Pane: <span class="stat-val">${sb.notional.toLocaleString()} tokens</span></div>
    <div class="stat-row">Saving: <span class="stat-val" style="color:#3fb950">${sb.saving_pct}%</span></div>
  `;
  document.getElementById('model-name').textContent = sb.model;
}

// Load initial state
fetch('/api/state').then(r => r.json()).then(data => {
  updateSidebar(data);
  document.getElementById('model-name').textContent = data.model;
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/state")
async def get_state():
    return JSONResponse(build_sidebar_state())


@app.post("/api/chat")
async def chat(request: Request):
    global recent_messages, turn_count, total_in, total_out

    body = await request.json()
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # 1. Tick TTL
    tick_ttl(db)

    # 1b. Soft-load recalled topics (cross-session reload)
    from pane.recall import recall as _recall
    from pane.schema import soft_load_recalled
    _result = _recall(user_msg, db)
    _matched = [t["id"] for t, _ in _result.topics[:5]] if _result.topics else []
    if _matched:
        soft_load_recalled(db, _matched)

    # 2. Build managed context
    memory_block = build_context(db)

    # 3. Assemble messages
    messages = []
    if memory_block:
        messages.append({
            "role": "user",
            "content": f"[CONTEXT — background memory, do not respond to this]\n{memory_block}"
        })
        messages.append({
            "role": "assistant",
            "content": "Understood, I have the background context."
        })
    messages.extend(recent_messages[-RAW_MESSAGE_WINDOW:])
    messages.append({"role": "user", "content": user_msg})

    # 4. Call API
    response = client.messages.create(
        model=model_name,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=messages,
        max_tokens=4000,
    )

    assistant_text = response.content[0].text
    in_tokens = response.usage.input_tokens
    out_tokens = response.usage.output_tokens
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    total_in += in_tokens
    total_out += out_tokens
    turn_count += 1

    # 5. Extract metadata
    metadata, clean_text = extract_turn_json(assistant_text)

    # 6. Process through pipeline
    if metadata:
        action = process_metadata(db, window_id, user_msg, assistant_text, metadata)
    else:
        from pane.schema import save_messages
        save_messages(db, window_id, [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": assistant_text},
        ])
        action = "no-metadata"

    # 7. Track recent messages
    recent_messages.append({"role": "user", "content": user_msg})
    recent_messages.append({"role": "assistant", "content": clean_text})

    # 8. Append to readable chat log
    log_path = Path(db.execute("PRAGMA database_list").fetchone()[2]).with_suffix(".log.md")
    with open(log_path, "a", encoding="utf-8") as f:
        if turn_count == 1:
            f.write(f"# Pane Chat Log\n\nModel: {model_name}\n\n---\n\n")
        loaded = get_loaded_topics_with_ttl(db)
        active = get_entities_from_loaded_topics(db)
        f.write(f"### Turn {turn_count}\n\n")
        f.write(f"**You:** {user_msg}\n\n")
        f.write(f"**Claude:** {clean_text}\n\n")
        f.write(f"*[{action}] {in_tokens:,} in / {out_tokens:,} out "
                f"| loaded: {len(loaded)} topics | active: {', '.join(active[:5]) or 'none'}*\n\n---\n\n")

    # 8. Build meta string
    cache_str = f"hit:{cache_read}" if cache_read else (
        f"write:{cache_create}" if cache_create else "none")
    meta = f"[{action}] {in_tokens:,} in / {out_tokens:,} out | cache: {cache_str}"

    return JSONResponse({
        "response": clean_text,
        "meta": meta,
        "sidebar": build_sidebar_state(),
    })


def build_sidebar_state():
    loaded = get_loaded_topics_with_ttl(db)
    active = get_entities_from_loaded_topics(db)
    facts = get_facts_for_entities(db, [USER_ENTITY] + active)
    nt = notional_tokens(db)

    topics = []
    for tid, ttl in loaded:
        t = db.execute(
            "SELECT title, category_fingerprint FROM topics WHERE id = ?",
            (tid,)
        ).fetchone()
        if t:
            topics.append({
                "title": t["title"],
                "categories": t["category_fingerprint"] or "",
                "ttl": ttl,
            })

    # Pane context = memory block + recent raw messages (what we actually send)
    memory_block = build_context(db)
    pane_tokens = len(memory_block) // 4
    # Add recent raw messages estimate
    for msg in recent_messages[-RAW_MESSAGE_WINDOW:]:
        pane_tokens += len(msg.get("content", "")) // 4
    saving_pct = round((1 - pane_tokens / nt) * 100) if nt > 0 else 0

    return {
        "topics": topics,
        "active_entities": active,
        "facts": {k: v for k, v in facts.items()},
        "turn": turn_count,
        "tokens_in": total_in,
        "tokens_out": total_out,
        "notional": nt,
        "pane_tokens": pane_tokens,
        "saving_pct": max(0, saving_pct),
        "cache": "n/a",
        "model": model_name or "",
    }


def main():
    global client, db, window_id, model_name, system_prompt

    parser = argparse.ArgumentParser(description="Pane web chat UI")
    parser.add_argument("--db", default="pane_web.db")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--system", default=None)
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = Anthropic()
    db = create_db(args.db)
    window_id = create_window(db)
    model_name = args.model
    system_prompt = (args.system or "You are a helpful assistant.") + PANE_SYSTEM_SUFFIX

    print(f"Pane web UI | http://localhost:{args.port} | model: {model_name}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
