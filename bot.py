#!/usr/bin/env python3
"""Telegram bot listener — handles your messages and updates agent memory."""

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from telegram import Update
from telegram.error import Conflict
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

from utils import (
    BASE_DIR,
    load_env_file,
    load_memory,
    save_memory,
    ask_ollama,
    acquire_single_instance_lock,
    setup_logging,
    RedactTokenFilter,
)

BOT_LOCK = BASE_DIR / ".bot.lock"

# Apply token redaction to all handlers
for handler in logging.getLogger().handlers:
    handler.addFilter(RedactTokenFilter())


def extract_action(text):
    """Find the first valid action JSON object in text.

    Scans for balanced {...} spans (handles nested braces and arrays, which the
    old `\\{[^{}]*\\}` regex could not), then returns the first one that parses
    as JSON and carries an "action" key. Returns None if none found.
    """
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                    except json.JSONDecodeError:
                        start = None
                        continue
                    if isinstance(obj, dict) and "action" in obj:
                        return obj, (start, i + 1)
                    start = None
    return None


def apply_action(mem, action):
    """Apply a parsed action dict to memory. Returns confirmation message."""
    act = action.get("action")

    if act == "update_topics":
        mem["news_topics"] = action["value"]
        return f"Updated news topics to: {', '.join(action['value'])}"

    elif act == "add_topic":
        topics = mem.setdefault("news_topics", [])
        if action["topic"] not in topics:
            topics.append(action["topic"])
        return f"Added topic: {action['topic']}"

    elif act == "remove_topic":
        before = len(mem.get("news_topics", []))
        mem["news_topics"] = [
            t for t in mem.get("news_topics", [])
            if action["topic"].lower() not in t.lower()
        ]
        removed = before - len(mem["news_topics"])
        return f"Removed {removed} topic(s) matching '{action['topic']}'"

    elif act == "add_channel":
        channels = mem.setdefault("youtube_channels", [])
        channels.append({"name": action["name"], "url": action["url"], "last_video_id": None})
        return f"Added YouTube channel: {action['name']}"

    elif act == "remove_channel":
        before = len(mem.get("youtube_channels", []))
        mem["youtube_channels"] = [
            c for c in mem.get("youtube_channels", [])
            if action["name"].lower() not in c["name"].lower()
        ]
        removed = before - len(mem["youtube_channels"])
        return f"Removed {removed} channel(s) matching '{action['name']}'"

    elif act == "add_rule":
        rules = mem.setdefault("rules", [])
        rules.append(action["rule"])
        return f"Added rule: {action['rule']}"

    elif act == "clear_rules":
        mem["rules"] = []
        return "Cleared all rules."

    elif act == "show_config":
        return (
            f"*Current Config*\n"
            f"News topics: {', '.join(mem.get('news_topics', []))}\n"
            f"YouTube channels: {', '.join(c['name'] for c in mem.get('youtube_channels', []))}\n"
            f"Rules: {chr(10).join(mem.get('rules', [])) or 'none'}"
        )

    elif act == "run_digest":
        agent_script = BASE_DIR / "agent.py"
        subprocess.Popen([sys.executable, str(agent_script)])
        return "Running digest now — you'll get it in about a minute."

    return None


DIGEST_TRIGGERS = {
    "refresh", "re-run", "rerun", "run digest", "send digest",
    "latest news", "get news", "update feed", "refresh feed",
    "send me the latest", "new digest", "run now", "fetch news",
}


def is_authorized(update: Update) -> bool:
    """Check if sender is allowed. If no restriction configured, allow all."""
    allowed_username = os.getenv("TELEGRAM_ALLOWED_USERNAME", "").strip().lstrip("@").lower()
    allowed_user_id = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not allowed_username and not allowed_user_id:
        return True
    user = update.effective_user
    if not user:
        return False
    if allowed_username and user.username and user.username.lower() == allowed_username:
        return True
    if allowed_user_id and str(user.id) == allowed_user_id:
        return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        logging.warning(f"Unauthorized message from user_id={update.effective_user.id if update.effective_user else 'unknown'}")
        await update.message.reply_text("Unauthorized.")
        return

    mem = load_memory()
    user_msg = update.message.text
    model = mem.get("ollama_model", "llama3.2")

    # Short-circuit: trigger digest without hitting Ollama
    if any(trigger in user_msg.lower() for trigger in DIGEST_TRIGGERS):
        await update.message.reply_text("Running digest now — you'll get it in about a minute.")
        subprocess.Popen([str(BASE_DIR / "venv/bin/python3"), str(BASE_DIR / "agent.py")])
        return

    current_config = json.dumps({
        "news_topics": mem.get("news_topics"),
        "youtube_channels": [c["name"] for c in mem.get("youtube_channels", [])],
        "rules": mem.get("rules", []),
    }, indent=2)

    system = """You are a personal AI agent assistant. The user can chat with you, ask questions,
or update your configuration.

If the user wants to change settings, include a JSON action block anywhere in your response using this format:
{"action": "add_topic", "topic": "..."}
{"action": "remove_topic", "topic": "..."}
{"action": "update_topics", "value": ["...", "..."]}
{"action": "add_channel", "name": "...", "url": "..."}
{"action": "remove_channel", "name": "..."}
{"action": "add_rule", "rule": "..."}
{"action": "clear_rules"}
{"action": "show_config"}
{"action": "run_digest"}

Use {"action": "run_digest"} if the user asks to re-run the digest, refresh the feed, get latest news, or similar.

Otherwise just respond conversationally as a helpful assistant.
Keep responses concise.
CRITICAL: When using Markdown (*bold*, _italic_), every opening marker must have a matching closing marker.
Do not use * or _ inside regular words unless they are part of a complete pair."""

    response = ask_ollama(
        f"Current agent config:\n{current_config}\n\nUser says: {user_msg}",
        system=system,
        model=model,
    )

    if response is None:
        await update.message.reply_text(
            "Sorry, I'm having trouble connecting to the AI model right now. Please try again in a moment."
        )
        return

    # Parse any action block (brace-balanced scan handles nested JSON/arrays)
    extracted = extract_action(response)
    confirmation = None
    clean_response = response
    if extracted:
        action, (span_start, span_end) = extracted
        try:
            confirmation = apply_action(mem, action)
            if confirmation:
                save_memory(mem)
            else:
                logging.warning(f"Unknown or no-op action from model: {action!r}")
                confirmation = "I couldn't apply that change — unrecognized command."
        except (KeyError, TypeError) as e:
            logging.warning(f"Malformed action {action!r}: {e}")
            confirmation = "I understood you wanted a config change but the command was malformed."
        # Strip the matched JSON span from the user-facing text, plus any
        # now-empty code fence the action JSON was wrapped in.
        clean_response = (response[:span_start] + response[span_end:]).strip()
        clean_response = re.sub(
            r"```[a-zA-Z]*\s*```", "", clean_response
        ).strip()

    reply_parts = []
    if clean_response:
        reply_parts.append(clean_response)
    if confirmation:
        reply_parts.append(f"✓ {confirmation}")

    await update.message.reply_text("\n\n".join(reply_parts) or "Done!")


async def digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text("Running digest now — you'll get it in about a minute.")
    subprocess.Popen([str(BASE_DIR / "venv/bin/python3"), str(BASE_DIR / "agent.py")])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "AI Agent online. I'll send you digests at 10am and 10pm.\n\n"
        "You can tell me things like:\n"
        "- 'Add AI startups to my news topics'\n"
        "- 'Add YouTube channel: Fireship, https://youtube.com/@Fireship'\n"
        "- 'Always keep summaries under 3 bullets'\n"
        "- 'Show my current config'"
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        logging.error("Telegram polling conflict: another bot instance is already running.")
        return
    logging.exception("Unhandled bot error", exc_info=err)


def main():
    _lock = acquire_single_instance_lock(BOT_LOCK)
    setup_logging("bot")
    load_env_file()
    mem = load_memory()
    token = os.getenv("TELEGRAM_TOKEN") or mem.get("telegram_token")
    if not token:
        raise SystemExit("Missing TELEGRAM_TOKEN. Set it in .env or memory.json.")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("digest", digest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)
    logging.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
