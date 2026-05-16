#!/usr/bin/env python3
"""Shared utilities for AI agent — env, memory, LLM, locks."""

import atexit
import fcntl
import json
import logging
import os
import re
import requests
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).parent
MEMORY_FILE = BASE_DIR / "memory.json"
HISTORY_FILE = BASE_DIR / "history.json"
ENV_FILE = BASE_DIR / ".env"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
MAX_TELEGRAM_MSG_LEN = 4096


def repair_markdown_line(line: str) -> str:
    """Repair mismatched bold/italic markers on a single line.

    Small LLMs frequently emit a span that opens with one marker and closes
    with the other — e.g. `*Title_`. The plain odd-count strip cannot fix that
    (both markers are odd, so it would delete both and lose the styling). Here
    we treat * and _ as one combined set of markers: pair them up positionally
    and rewrite each pair to match the marker that opened it. A leftover
    unpaired marker is dropped.
    """
    positions = [(i, ch) for i, ch in enumerate(line) if ch in "*_"]
    if len(positions) < 2:
        # 0 markers: nothing to do. 1 marker: dangling — drop it.
        if len(positions) == 1:
            i = positions[0][0]
            return line[:i] + line[i + 1 :]
        return line

    chars = list(line)
    drop = set()
    # Pair markers two-by-two in document order; the closer becomes the opener.
    for a, b in zip(positions[0::2], positions[1::2]):
        opener_idx, opener_ch = a
        closer_idx, _ = b
        chars[closer_idx] = opener_ch
    # Odd one out (unpaired trailing marker) — drop it.
    if len(positions) % 2 == 1:
        drop.add(positions[-1][0])
    return "".join(c for i, c in enumerate(chars) if i not in drop)


def sanitize_telegram_markdown(text: str) -> str:
    """Make Telegram Markdown safe: repair mismatched/dangling * and _ markers.

    Operates line by line — Telegram bold/italic spans do not cross newlines,
    so pairing per line both fixes more cases and avoids a marker on one line
    being matched against one on another.
    """
    return "\n".join(repair_markdown_line(line) for line in text.split("\n"))


def setup_logging(name: str, level=logging.INFO):
    """Set up rotating file + console logging."""
    logger = logging.getLogger()
    logger.setLevel(level)
    # Clear existing handlers to avoid duplicates on reload
    logger.handlers = []

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # Rotating file handler: 2MB per file, keep 3 backups
    file_handler = RotatingFileHandler(
        BASE_DIR / f"{name}.log", maxBytes=2 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    logging.getLogger("telegram.bot").setLevel(logging.WARNING)
    return logger


class RedactTokenFilter(logging.Filter):
    _token_re = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")

    def filter(self, record):
        message = record.getMessage()
        sanitized = self._token_re.sub("bot<redacted>", message)
        if sanitized != message:
            record.msg = sanitized
            record.args = ()
        return True


def load_env_file():
    """Load simple KEY=VALUE pairs from .env into process env if missing."""
    if not ENV_FILE.exists():
        return
    with open(ENV_FILE) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _atomic_write_json(path: Path, data):
    """Write JSON atomically (.tmp then rename), keeping a .bak of the prior file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    bak = path.with_suffix(path.suffix + ".bak")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        if path.exists():
            shutil.copy2(path, bak)
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def load_memory():
    with open(MEMORY_FILE) as f:
        return json.load(f)


def save_memory(mem):
    """Atomic write with backup. Writes to .tmp then rename; keeps .bak."""
    _atomic_write_json(MEMORY_FILE, mem)


HISTORY_MAX = 150


def load_history():
    """Load the rolling list of already-sent news articles. Empty list if absent."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logging.warning(f"Could not read history.json ({e}); starting fresh.")
        return []


def save_history(history):
    """Persist news history, newest-first, capped at HISTORY_MAX entries."""
    _atomic_write_json(HISTORY_FILE, history[:HISTORY_MAX])


def acquire_single_instance_lock(lock_path: Path):
    """Acquire an exclusive flock-based lock. Raises SystemExit if already locked."""
    lock_handle = open(lock_path, "w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(f"Another instance is already running (lock: {lock_path}).")
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    atexit.register(lock_handle.close)
    return lock_handle


def ask_ollama(prompt, system=None, model="llama3.2"):
    """Call local Ollama. Returns None on failure after retries."""
    ollama_url = os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_URL)
    payload = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    for attempt in range(2):
        try:
            r = requests.post(ollama_url, json=payload, timeout=300)
            r.raise_for_status()
            return r.json()["response"].strip()
        except Exception as e:
            logging.warning(f"Ollama error (attempt {attempt + 1}): {e}")
    return None


def send_telegram(token, chat_id, message, parse_mode="Markdown"):
    """Send message to Telegram. Falls back to plain text if Markdown parse fails."""
    if parse_mode == "Markdown":
        message = sanitize_telegram_markdown(message)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(message), MAX_TELEGRAM_MSG_LEN):
        chunk = message[i : i + MAX_TELEGRAM_MSG_LEN]
        for mode in (parse_mode, None):
            payload = {"chat_id": chat_id, "text": chunk}
            if mode:
                payload["parse_mode"] = mode
            try:
                r = requests.post(url, json=payload, timeout=15)
                r.raise_for_status()
                resp = r.json()
                if not resp.get("ok"):
                    desc = resp.get("description", "").lower()
                    # Telegram often returns 200 with ok:false for parse errors
                    if mode and ("parse" in desc or "can't find end" in desc or "entity" in desc):
                        logging.warning(f"Markdown parse failed ({desc}), retrying as plain text.")
                        continue
                    logging.warning(f"Telegram API error: {resp}")
                else:
                    break
            except requests.HTTPError as e:
                if mode and e.response.status_code == 400:
                    logging.warning("Markdown parse failed (HTTP 400), retrying as plain text.")
                    continue
                logging.error(f"Telegram send error: {e}")
                break
            except Exception as e:
                logging.error(f"Telegram send error: {e}")
                break


def normalize_url_for_dedup(url: str) -> str:
    """Strip tracking params and fragments for deduplication."""
    from urllib.parse import urlparse, parse_qs, urlencode
    try:
        p = urlparse(url)
        # Keep only meaningful query params
        q = parse_qs(p.query)
        keep = {k: v for k, v in q.items() if k.lower() not in {
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "gclid", "ref", "source"
        }}
        qstr = urlencode(keep, doseq=True)
        return f"{p.scheme}://{p.netloc}{p.path}" + (f"?{qstr}" if qstr else "")
    except Exception:
        return url
