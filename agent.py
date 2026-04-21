#!/usr/bin/env python3
"""Daily AI agent — fetches news + YouTube summaries, sends via Telegram."""

import logging
import os
import re
import trafilatura
from datetime import datetime
from ddgs import DDGS
from pathlib import Path
from urllib.parse import urlparse
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp

from utils import (
    BASE_DIR,
    load_env_file,
    load_memory,
    save_memory,
    ask_ollama,
    send_telegram,
    acquire_single_instance_lock,
    setup_logging,
    normalize_url_for_dedup,
)

AGENT_LOCK = BASE_DIR / ".agent.lock"


def fetch_article_content(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if text:
                return text[:3000]
    except Exception:
        pass
    try:
        import requests
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = re.sub(r'<script[^>]*>.*?</script>', '', r.text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 200:
            return text[:3000]
    except Exception:
        pass
    logging.warning(f"Could not extract content from {url}")
    return None


def normalize_source_name(source, url):
    source = (source or "").strip()
    hostname = urlparse(url).netloc.lower()
    hostname = hostname.removeprefix("www.")
    if source.lower().endswith(" on msn") and hostname != "msn.com":
        return source[:-7].strip()
    if source:
        return source
    return hostname or "Unknown source"


def is_instruction_topic(topic):
    lowered = topic.lower()
    return any(marker in lowered for marker in ("summary format", "bold text", "italic text", "indentation"))


def is_low_signal_article(article):
    title = article["title"].lower()
    source = article["source"].lower()
    low_signal_patterns = (
        "coolest", "top 10", "top ten", "roundup", "best ai",
        "products of 2025", "products of 2026",
    )
    return any(pattern in title for pattern in low_signal_patterns) or source == "crn"


def fetch_news(topics, max_total=6):
    """Fetch news with round-robin topic distribution and normalized dedup."""
    articles_by_topic = []
    seen_urls = set()
    seen_titles = set()

    with DDGS() as ddgs:
        for topic in topics:
            if is_instruction_topic(topic):
                continue
            topic_articles = []
            try:
                for r in ddgs.news(topic, max_results=4):
                    url = r.get("url", "")
                    title = re.sub(r"\s+", " ", r.get("title", "")).strip()
                    title_key = re.sub(r"[^\w]", "", title.lower())
                    url_key = normalize_url_for_dedup(url)
                    if not url or url_key in seen_urls or title_key in seen_titles:
                        continue
                    seen_urls.add(url_key)
                    seen_titles.add(title_key)
                    article = {
                        "title": title,
                        "url": url,
                        "snippet": r.get("body", "")[:400],
                        "source": normalize_source_name(r.get("source", ""), url),
                        "topic": topic,
                    }
                    if not is_low_signal_article(article):
                        topic_articles.append(article)
            except Exception as e:
                logging.warning(f"News fetch error for topic '{topic}': {e}")
            articles_by_topic.append(topic_articles)

    # Round-robin: take 1 from each topic until max_total or exhaustion
    result = []
    idx = 0
    while len(result) < max_total:
        added = False
        for t_articles in articles_by_topic:
            if idx < len(t_articles) and len(result) < max_total:
                result.append(t_articles[idx])
                added = True
        if not added:
            break
        idx += 1

    logging.info(f"Fetched {len(result)} articles across {len(topics)} topics.")
    return result


def build_news_digest(articles, model, rules_ctx):
    """Summarize articles in a single Ollama call with per-article fallback."""
    articles_text = ""
    for i, a in enumerate(articles, 1):
        content = fetch_article_content(a["url"]) or a["snippet"]
        evidence_note = "full article extract" if content and content != a["snippet"] else "headline/snippet only"
        articles_text += (
            f"\n---\n"
            f"ARTICLE {i}\n"
            f"Title: {a['title']}\n"
            f"Source: {a['source']}\n"
            f"URL: {a['url']}\n"
            f"Evidence quality: {evidence_note}\n"
            f"Text:\n{content}\n"
        )

    prompt = (
        f"Here are today's AI news articles:{articles_text}\n\n"
        f"For each article, write in exactly this format:\n\n"
        f"*Exact article title*\n"
        f"_Source Name_\n"
        f"- What happened: one concrete sentence with the main claim, actor, and action.\n"
        f"- Why it matters: one concrete sentence explaining significance for AI, business, policy, or users.\n"
        f"- *Key takeaway:* one short sentence.\n\n"
        f"Rules:\n"
        f"- Do not write 'Article 1', 'Article 2', or similar labels.\n"
        f"- Do not use vague phrases like 'significant advancements' or 'continues to invest heavily' unless the evidence explicitly supports them.\n"
        f"- If evidence quality is 'headline/snippet only', be conservative and say only what is clearly supported.\n"
        f"- Prefer concrete nouns, names, products, dates, and institutions over marketing language.\n"
        f"- Keep each article to 3 bullets total.\n"
        f"- Separate articles with a blank line.{rules_ctx}"
    )
    system = (
        "You are a precise tech journalist writing for Telegram (Markdown). "
        "Use *bold* and _italic_ formatting. Be factual, specific, and skeptical of hype. No vague generalities. "
        "CRITICAL: Every * must have a matching closing *. Every _ must have a matching closing _. "
        "Do not use * or _ inside titles, quotes, or regular text unless they are part of a complete bold/italic pair."
    )

    summary = ask_ollama(prompt, system=system, model=model)
    if not summary:
        logging.warning("Batch news summarization failed; falling back to snippets.")
        lines = []
        for a in articles:
            lines.append(f"*{a['title']}*")
            lines.append(f"_{a['source']}_")
            lines.append(f"- {a['snippet']}")
            lines.append("")
        summary = "\n".join(lines)
    return summary, articles


def get_latest_videos(channel_url, max_videos=3):
    """Return list of (video_id, title) for the latest N videos."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1-{max_videos}",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"{channel_url}/videos", download=False)
            if info and "entries" in info and info["entries"]:
                return [
                    (e.get("id"), e.get("title", "Untitled"))
                    for e in info["entries"]
                    if e.get("id")
                ]
    except Exception as e:
        logging.warning(f"yt-dlp error for {channel_url}: {e}")
    return []


def get_transcript(video_id):
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        text = " ".join([t["text"] for t in transcript])
        return text[:6000]
    except Exception:
        return None


def summarize_video(title, transcript, model, rules_ctx):
    prompt = (
        f"Video: {title}\n\nTranscript:\n{transcript}\n\n"
        f"Write a detailed summary with these sections:\n\n"
        f"*Core Ideas*\n"
        f"- The 2-3 main arguments or frameworks presented. Be specific.\n\n"
        f"*Notable Quotes*\n"
        f"- Extract 2-3 of the most impactful direct quotes from the transcript. Use _italics_ for each quote.\n\n"
        f"*Why This Matters in the AI Era*\n"
        f"- 1-2 sentences connecting the video's ideas to current AI trends, work, creativity, or technology.\n\n"
        f"Use *bold* for section headers and key terms. Use _italics_ for quotes. "
        f"Be specific — include examples, names, or numbers.{rules_ctx}"
    )
    system = (
        "Summarize this YouTube video for Telegram (Markdown). "
        "Extract real substance: core ideas, direct quotes, and relevance to the AI world. No fluff. "
        "CRITICAL: Every * must have a matching closing *. Every _ must have a matching closing _. "
        "Do not use * or _ inside titles, quotes, or regular text unless they are part of a complete bold/italic pair."
    )
    return ask_ollama(prompt, system=system, model=model)


def main():
    _lock = acquire_single_instance_lock(AGENT_LOCK)
    setup_logging("agent")
    load_env_file()
    mem = load_memory()
    token = os.getenv("TELEGRAM_TOKEN") or mem.get("telegram_token")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or mem.get("telegram_chat_id")
    if not token or not chat_id:
        raise SystemExit("Missing TELEGRAM_TOKEN / TELEGRAM_CHAT_ID. Set them in .env or memory.json.")

    topics = mem.get("news_topics", ["AI news"])
    channels = mem.get("youtube_channels", [])
    model = mem.get("ollama_model", "llama3.2")
    rules = mem.get("rules", [])

    now = datetime.now().strftime("%A, %B %d — %I:%M %p")
    rules_ctx = ("\n\nExtra instructions:\n" + "\n".join(f"- {r}" for r in rules)) if rules else ""

    sections = [f"*Your AI Digest*\n_{now}_\n{'─' * 28}"]

    # --- News ---
    sections.append("\n*AI NEWS*\n")
    logging.info("Fetching news...")
    articles = fetch_news(topics)
    if articles:
        logging.info(f"Summarizing {len(articles)} articles...")
        news_digest, articles = build_news_digest(articles, model, rules_ctx)
        sections.append(news_digest)
        sections.append("\n*Sources*")
        for i, a in enumerate(articles, 1):
            sections.append(f"{i}. [{a['title']}]({a['url']})")
    else:
        sections.append("_(Could not fetch news today)_")

    sections.append("\n" + "─" * 28)

    # --- YouTube ---
    for channel in channels:
        logging.info(f"Checking {channel['name']}...")
        videos = get_latest_videos(channel["url"], max_videos=3)
        if not videos:
            sections.append(f"\n*{channel['name'].upper()}*\n_(Could not fetch latest video)_")
            continue

        sections.append(f"\n*{channel['name'].upper()}*\n")
        last_id = channel.get("last_video_id")
        new_videos = [v for v in videos if v[0] != last_id]

        if not new_videos:
            sections.append("No new video since last digest.")
            continue

        # Summarize up to 2 newest videos to avoid digest bloat
        for vid_id, vid_title in new_videos[:2]:
            video_url = f"https://youtube.com/watch?v={vid_id}"
            transcript = get_transcript(vid_id)
            if transcript:
                logging.info(f"Summarizing video: {vid_title}...")
                summary = summarize_video(vid_title, transcript, model, rules_ctx)
                if summary:
                    sections.append(f"*{vid_title}*\n{video_url}\n\n{summary}\n")
                else:
                    sections.append(
                        f"*{vid_title}*\n{video_url}\n_(Summarization unavailable — watch the video directly)_\n"
                    )
            else:
                sections.append(f"*{vid_title}*\n{video_url}\n_(No transcript available)_\n")

        # Update last_video_id to the absolute latest
        channel["last_video_id"] = videos[0][0]
        sections.append("─" * 28)

    save_memory(mem)
    logging.info("Sending to Telegram...")
    send_telegram(token, chat_id, "\n".join(sections))
    logging.info("Digest sent.")


if __name__ == "__main__":
    main()
