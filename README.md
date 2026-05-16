# AI News Reporter

Personal AI agent that fetches news and YouTube video summaries, then delivers them via Telegram on a cron schedule. Summarization runs locally through Ollama — nothing leaves your machine except the final digest.

## Features

- **Daily digests** at 10am/10pm with news from your chosen topics
- **A digest with a voice** — written as a warm, lightly opinionated tech friend, not a dry wire service; each story gets a "worth your time?" verdict
- **Relevance ranking** — fetches a wider candidate pool, then scores and keeps the most relevant stories
- **Self-verification** — cross-checks each digest against its source articles and flags unsupported claims before sending
- **No repeats** — `history.json` remembers sent stories so digests never recycle the same news
- **YouTube channel monitoring** — checks for new videos and summarizes transcripts
- **Natural-language config** — talk to the bot in Telegram to add topics, channels, or rules
- **Local LLM** — uses Ollama (`qwen2.5:7b` by default); no API keys beyond Telegram
- **Safe persistence** — atomic writes to `memory.json` / `history.json` with automatic `.bak`
- **Access control** — optional username/user-ID allowlist

## Architecture

| File | Role |
|---|---|
| `agent.py` | Core digest pipeline: news (DuckDuckGo) + YouTube transcripts → Ollama → Telegram |
| `bot.py` | Telegram listener; parses natural-language commands and updates `memory.json` |
| `utils.py` | Shared: env loader, atomic memory I/O, Ollama client, Telegram sender, rotating logs |
| `setup.sh` | Interactive first-time setup (venv, token, chat ID, cron) |
| `start_bot.sh` | Launches Ollama + bot listener |
| `memory.json` | Runtime config: topics, channels, rules, model, per-channel `last_video_id` |
| `history.json` | Rolling record of already-sent articles (cross-run dedup); auto-managed |

## Setup

```bash
# One-time setup (creates venv, prompts for Telegram creds, installs cron)
./setup.sh

# Start the bot listener
./start_bot.sh

# Or run a digest manually
source venv/bin/activate && python3 agent.py
```

If you skip `setup.sh`, create `.env` manually:

```
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
# Optional:
# OLLAMA_URL=http://localhost:11434/api/generate
# TELEGRAM_ALLOWED_USERNAME=your_telegram_username
# TELEGRAM_ALLOWED_USER_ID=your_numeric_user_id
```

## Requirements

- Python 3 + `./venv`
- Ollama running locally on port 11434, with a model pulled (`ollama pull qwen2.5:7b`)
- `requests`, `python-telegram-bot`, `youtube-transcript-api`, `yt-dlp`, `duckduckgo-search`, `trafilatura`

## Usage

Once the bot is running, message it on Telegram:

- *"Add tech news to my topics"*
- *"Follow this YouTube channel: https://youtube.com/@example"*
- *"Show my current config"*
- *"Only summarize videos longer than 10 minutes"*

Digests arrive automatically at 10am/10pm. Send `/digest` (or say *"refresh"*, *"latest news"*, etc.) to trigger one on demand.

## Debugging

Logs rotate at 2MB:

- `bot.log` — Telegram listener
- `agent.log` — digest runs
- `ollama.log` — model calls
