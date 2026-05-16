# AI Agent — Daily Digest Bot

Personal AI agent that fetches news and YouTube video summaries, then delivers them via Telegram.

## Architecture

- **`agent.py`** — Core digest pipeline. Fetches a news candidate pool (DuckDuckGo) and YouTube transcripts, then runs a four-stage pipeline — **fetch → rank → summarize → verify** — and sends to Telegram. Runs on cron (10am/10pm).
- **`bot.py`** — Telegram bot listener. Handles user messages, parses natural language commands via Ollama, updates config in `memory.json`.
- **`utils.py`** — Shared utilities: env loader, atomic JSON I/O with backup, Ollama client, Telegram sender with Markdown fallback/repair, rotating logs.
- **`.env`** — Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (and optional `OLLAMA_URL`). **Never commit.**
- **`memory.json`** — Runtime config: news topics, YouTube channels, rules, Ollama model, and channel state (`last_video_id`). Auto-backed up to `memory.json.bak` on every write.
- **`history.json`** — Rolling list of already-sent news articles (cap 150, newest-first). Seeds the dedup seen-sets so digests never repeat a story across runs. Gitignored runtime state.
- **`setup.sh`** — Interactive first-time setup (venv, Telegram token, chat ID, cron).
- **`start_bot.sh`** — Launches Ollama + bot listener.

## Digest pipeline (agent.py)

1. **Fetch** — `fetch_news()` pulls a candidate pool (~12) round-robin across topics; articles already in `history.json` are excluded.
2. **Rank** — `rank_articles()` scores candidates 1–10 for relevance and keeps the top 6. Scoring plays to a small model's strengths; falls back to fetch order on any failure.
3. **Summarize** — `build_news_digest()` summarizes in one Ollama call, in the agent's persona voice (see below).
4. **Verify** — `verify_digest()` cross-checks the summary against source texts and the reader profile; appends a caution note if unsupported claims are found. Never blocks delivery.

## Voice / persona

The digest is written as **James's warm-but-grounded tech friend**, not a wire service. Defined by two constants in `agent.py`:

- **`VOICE`** — the persona: friendly, lightly opinionated, concise, no forced metaphors, minimal emoji. Shared by news and YouTube summaries so the tone never switches mid-digest.
- **`JAMES_PROFILE`** — the *only* true personal facts the model may use about James. Fences the persona so a small model can't invent hobbies; `verify_digest()` also checks against it.

`time_of_day()` gives a morning/evening-aware greeting and pacing.

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
- Ollama running locally on port 11434 (default model: `qwen2.5:7b`; set via `ollama_model` in `memory.json`)
- Packages: `requests`, `python-telegram-bot`, `youtube-transcript-api`, `yt-dlp`, `duckduckgo-search`, `trafilatura`

## Security

- Set `TELEGRAM_ALLOWED_USERNAME` or `TELEGRAM_ALLOWED_USER_ID` in `.env` to prevent unauthorized users from interacting with the bot.
- `memory.json` is atomically written and backed up to `memory.json.bak` on every save.

## Key patterns

- `ask_ollama()` returns `None` on failure (retries once). Callers must handle `None` with fallback content — never send error strings to Telegram.
- **Graceful degradation** — every LLM-dependent stage (rank, summarize, verify) falls back rather than blocking delivery. Verification can flag, never suppress.
- News fetching uses round-robin distribution across topics and strips tracking parameters for deduplication. `history.json` extends dedup across runs.
- YouTube checks the latest 3 videos per channel and summarizes any new ones.
- `bot.py` parses model-emitted action JSON with `extract_action()` — a brace-balanced scan, not regex, so nested JSON/arrays parse correctly.
- Telegram messages are chunked at 4096 chars. If Markdown parse fails, the chunk is retried as plain text.
- `sanitize_telegram_markdown()` repairs mismatched/dangling `*` and `_` per line before sending — small models emit broken pairs like `*Title_`.
- `strip_signoff()` drops chatty closing lines the model adds despite the prompt forbidding them (prompts ask; code guarantees).
- Atomic JSON writes (`_atomic_write_json`): `.tmp` → rename, keeping a `.bak`. Used for both `memory.json` and `history.json`.
- `agent.py` uses `.agent.lock` to prevent overlapping digest runs. `bot.py` uses `.bot.lock`.
- Logs rotate automatically at 2MB (`bot.log`, `agent.log`).

## Common tasks

- **Add a news topic**: Tell the bot in Telegram or edit `memory.json` `news_topics` array
- **Add a YouTube channel**: Tell the bot or add to `memory.json` `youtube_channels` with `name`, `url`, `last_video_id`
- **Change model**: Update `ollama_model` in `memory.json`
- **Debug**: Check `bot.log`, `agent.log`, and `ollama.log`
