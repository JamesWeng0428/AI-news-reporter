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
ENV_FILE = BASE_DIR / ".env"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
MAX_TELEGRAM_MSG_LEN = 4096


def sanitize_telegram_markdown(text: str) -> str:
    """Remove dangling * or _ markers that would break Telegram Markdown parse."""
    for marker in ("*", "_"):
        while text.count(marker) % 2 != 0:
            idx = text.rfind(marker)
            if idx == -1:
                break
            text = text[:idx] + text[idx + 1 :]
    return text


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


def load_memory():
    with open(MEMORY_FILE) as f:
        return json.load(f)


def save_memory(mem):
    """Atomic write with backup. Writes to .tmp then rename; keeps .bak."""
    tmp = MEMORY_FILE.with_suffix(".json.tmp")
    bak = MEMORY_FILE.with_suffix(".json.bak")
    try:
        with open(tmp, "w") as f:
            json.dump(mem, f, indent=2)
        if MEMORY_FILE.exists():
            shutil.copy2(MEMORY_FILE, bak)
        tmp.replace(MEMORY_FILE)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


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
