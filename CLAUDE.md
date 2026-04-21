# AI Agent — Daily Digest Bot

Personal AI agent that fetches news and YouTube video summaries, then delivers them via Telegram.

## Architecture

- **`agent.py`** — Core digest pipeline. Fetches news (DuckDuckGo) and YouTube transcripts, summarizes via Ollama, sends to Telegram. Runs on cron (10am/10pm).
- **`bot.py`** — Telegram bot listener. Handles user messages, parses natural language commands via Ollama, updates config in `memory.json`.
- **`utils.py`** — Shared utilities: env loader, atomic memory I/O with backup, Ollama client, Telegram sender with Markdown fallback, rotating logs.
- **`.env`** — Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (and optional `OLLAMA_URL`). **Never commit.**
- **`memory.json`** — Runtime config: news topics, YouTube channels, rules, Ollama model, and channel state (`last_video_id`). Auto-backed up to `memory.json.bak` on every write.
- **`setup.sh`** — Interactive first-time setup (venv, Telegram token, chat ID, cron).
- **`start_bot.sh`** — Launches Ollama + bot listener.

## Running

```bash
# First time
./setup.sh

# Start bot listener (also starts Ollama if needed)
./start_bot.sh

# Run digest manually
source venv/bin/activate && python3 agent.py

# If not using setup.sh, create .env with:
# TELEGRAM_TOKEN=...
# TELEGRAM_CHAT_ID=...
```

## Dependencies

- Python 3 with venv at `./venv`
- Ollama running locally on port 11434 (model: `llama3.2`)
- Packages: `requests`, `python-telegram-bot`, `youtube-transcript-api`, `yt-dlp`, `duckduckgo-search`, `trafilatura`

## Security

- Set `TELEGRAM_ALLOWED_USERNAME` or `TELEGRAM_ALLOWED_USER_ID` in `.env` to prevent unauthorized users from interacting with the bot.
- `memory.json` is atomically written and backed up to `memory.json.bak` on every save.

## Key patterns

- `ask_ollama()` returns `None` on failure (retries once). Callers must handle `None` with fallback content — never send error strings to Telegram.
- News fetching uses round-robin distribution across topics and strips tracking parameters for deduplication.
- YouTube checks the latest 3 videos per channel and summarizes any new ones.
- Telegram messages are chunked at 4096 chars. If Markdown parse fails, the chunk is retried as plain text.
- `agent.py` uses `.agent.lock` to prevent overlapping digest runs. `bot.py` uses `.bot.lock`.
- Logs rotate automatically at 2MB (`bot.log`, `agent.log`).

## Common tasks

- **Add a news topic**: Tell the bot in Telegram or edit `memory.json` `news_topics` array
- **Add a YouTube channel**: Tell the bot or add to `memory.json` `youtube_channels` with `name`, `url`, `last_video_id`
- **Change model**: Update `ollama_model` in `memory.json`
- **Debug**: Check `bot.log`, `agent.log`, and `ollama.log`
