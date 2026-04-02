"""UserPromptSubmit hook — searches memory, injects context.

Runs BEFORE the LLM sees the user's message.
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'src'))

from pane.schema import create_db
from pane.recall import recall, load_context

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
    hook_input = json.loads(sys.stdin.read())
    prompt = hook_input.get("prompt", "")

    if not prompt or not os.path.exists(DB_PATH):
        print(json.dumps({}))
        return

    db = create_db(DB_PATH)
    result = recall(prompt, db)

    if result.mode == "fact":
        context = f"[MEMORY] {result.answer}"
    elif result.mode == "topic" and result.topics:
        topic_ids = [t["id"] for t, score in result.topics[:5]]
        raw = load_context(topic_ids, db)
        context = f"[MEMORY]\n{raw}" if raw else ""
    else:
        context = ""

    db.close()

    if context:
        tokens = len(context) // 4
        update_stats(recalls=1, tokens_injected=tokens)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({}))
