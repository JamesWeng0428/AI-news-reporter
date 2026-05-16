#!/usr/bin/env python3
"""Daily AI agent — fetches news + YouTube summaries, sends via Telegram."""

import json
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
    load_history,
    save_history,
    ask_ollama,
    send_telegram,
    acquire_single_instance_lock,
    setup_logging,
    normalize_url_for_dedup,
)

AGENT_LOCK = BASE_DIR / ".agent.lock"


def time_of_day():
    """Return ('morning'|'afternoon'|'evening', greeting) for voice context."""
    hour = datetime.now().hour
    if hour < 12:
        return "morning", "Good morning"
    if hour < 17:
        return "afternoon", "Afternoon"
    return "evening", "Evening"


_SIGNOFF_RE = re.compile(
    r"^\s*(hope (this|that|these)|that('s| is) (it|all)|happy reading|"
    r"enjoy your|have a (great|good)|stay (curious|tuned)|catch you|"
    r"let me know|until (next|tomorrow)|see you|cheers[!.]?$)",
    re.IGNORECASE,
)


def strip_signoff(text: str) -> str:
    """Drop a trailing chatty sign-off line if the model added one.

    The prompt forbids closing lines, but qwen2.5 still emits 'Hope these
    help ease you into the day!' style sign-offs. Prompts ask; this guarantees.
    Only the LAST non-empty line is considered, so real content is never cut.
    """
    lines = text.rstrip().split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _SIGNOFF_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


# Voice: a warm-but-grounded friend, not a wire service. Shared across
# every LLM call that produces reader-facing prose so the tone stays
# consistent. Substance and skepticism are unchanged — only the register.
VOICE = (
    "Voice: you are James's sharp, friendly, tech-savvy friend. James is a "
    "20-year-old UIUC computer science student who loves Claude and AI, and is "
    "into bouldering. You know him well. You are NOT James — you are a separate "
    "person texting him. Address him as 'James' or 'you', never as 'I/me'. "
    "Be warm and a little funny, with light opinions, but always grounded and "
    "concise. You call hype what it is. You never fawn, never pad, never use "
    "corporate phrases like 'in today's fast-paced world'. Talk to James like a "
    "friend who respects his time and his intelligence.\n"
    "Restraint matters — warmth is in the phrasing, not in performing:\n"
    "- Do NOT force climbing/bouldering, CS, or AI metaphors onto unrelated "
    "news. A metaphor only earns its place if it genuinely fits; otherwise skip it.\n"
    "- Emoji: rare. At most one in the whole digest, and only if it truly adds "
    "something. Usually use none.\n"
    "- No 'tickle your fancy', no 'dive in', no exclamation-point cheerleading. "
    "Dry, understated wit beats bubbly enthusiasm every time."
)

# The ONLY true facts the model may use about James. The persona invites the
# model to "relate news to James" — without this fence a small model invents
# hobbies (it once claimed James does "bouldering blockchain projects"). Any
# personal claim outside this list is a hallucination; verify_digest checks it.
JAMES_PROFILE = (
    "True facts about James (the ONLY personal facts you may state — never "
    "invent or speculate beyond these):\n"
    "- Computer Science student at UIUC (University of Illinois Urbana-Champaign).\n"
    "- A course assistant for CS124 (intro CS course).\n"
    "- A fan of Claude and of AI/LLM tools generally; likes building with them.\n"
    "- Into bouldering (rock climbing) as a hobby.\n"
    "If a news item does not naturally connect to one of these, do NOT force a "
    "personal angle — just explain why it matters in general terms."
)


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


def fetch_news(topics, max_total=12, history=None):
    """Fetch a candidate pool with round-robin topic distribution and dedup.

    Returns up to `max_total` articles; downstream ranking trims this to the
    final digest size, so the default pool is intentionally larger than the
    digest itself. Articles in `history` (already sent in past digests) are
    excluded — the seen-sets are seeded from history so cross-run repeats are
    filtered by the same logic as within-run duplicates.
    """
    articles_by_topic = []
    seen_urls = set()
    seen_titles = set()

    for h in history or []:
        if h.get("url_key"):
            seen_urls.add(h["url_key"])
        if h.get("title_key"):
            seen_titles.add(h["title_key"])
    if seen_urls:
        logging.info(f"Loaded {len(seen_urls)} previously-sent articles from history.")

    with DDGS() as ddgs:
        for topic in topics:
            if is_instruction_topic(topic):
                continue
            topic_articles = []
            try:
                for r in ddgs.news(topic, max_results=6):
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
                        "url_key": url_key,
                        "title_key": title_key,
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
    """Summarize articles in a single Ollama call with per-article fallback.

    Returns (summary, articles, sources) — `sources` is a list of the source
    text used per article, so a downstream verification pass can reuse it
    without re-fetching every URL.
    """
    articles_text = ""
    sources = []
    for i, a in enumerate(articles, 1):
        content = fetch_article_content(a["url"]) or a["snippet"]
        sources.append(content)
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

    period, _ = time_of_day()
    pacing = (
        "It is morning — open like someone easing James into his day."
        if period == "morning"
        else "It is evening — open like someone helping James wind down and catch up."
    )

    prompt = (
        f"Here are today's AI news articles:{articles_text}\n\n"
        f"Write James a short news digest. {pacing}\n\n"
        f"OUTPUT STRUCTURE — follow it exactly:\n\n"
        f"LINE 1 is ONE warm, opinionated lead line: a friend's honest take on the "
        f"day's news as a whole (what's interesting vs. noise). It is NOT an article. "
        f"It is plain text — no *bold*, no _italic_ on this line.\n"
        f"Do NOT greet James or say 'hi/hey/good morning' — a greeting is already "
        f"shown above your text. Start straight into the take on the news.\n\n"
        f"Then a blank line, then one block PER ARTICLE in exactly this 5-line shape:\n\n"
        f"*Exact article title here*\n"
        f"_Source Name here_\n"
        f"- What happened: one concrete sentence with the main claim, actor, and action.\n"
        f"- Why it matters: one concrete sentence — and where natural, connect it to "
        f"James's world (see his profile above).\n"
        f"- *Worth your time?* one short, honest verdict — must-read, skim, or skip, and why.\n\n"
        f"Then a blank line before the next article block.\n\n"
        f"Rules:\n"
        f"- The FIRST line of every article block is the title in *bold* — never the source.\n"
        f"- The SECOND line is the source in _italics_ — a separate line from the title.\n"
        f"- Use the exact article titles given above; do not rename them.\n"
        f"- Do not write 'Article 1', 'Article 2', or similar labels.\n"
        f"- Be a friend, not a press release: light opinions are welcome, hype is not.\n"
        f"- Do not invent facts. If evidence quality is 'headline/snippet only', be "
        f"conservative and say only what is clearly supported — your opinion can still "
        f"be honest ('not much detail here yet').\n"
        f"- Prefer concrete nouns, names, products, dates, and institutions over marketing language.\n"
        f"- Keep each article to exactly 3 bullets.\n"
        f"- End after the last article block. No closing line, sign-off, or 'hope this helps'.{rules_ctx}"
    )
    system = (
        f"{VOICE}\n\n"
        f"{JAMES_PROFILE}\n\n"
        "You are writing a news digest for Telegram (Markdown). Be factual, "
        "specific, and skeptical of hype — the warmth is in the voice, never in "
        "inventing or inflating facts.\n"
        "FORMATTING (Telegram Markdown — follow exactly):\n"
        "- Bold is *between single asterisks*. Italic is _between single underscores_.\n"
        "- Every * must be closed by another *. Every _ must be closed by another _.\n"
        "- NEVER mix them: a span that opens with * must close with *, not _.\n"
        "- An article title line is bold only: *Title here* — no underscore anywhere on it.\n"
        "- Do not use * or _ inside words or titles unless they form a complete pair."
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
    return strip_signoff(summary), articles, sources


CAUTION_NOTE = (
    "\n\n_⚠️ Some details above may not be fully supported by the source articles "
    "— click through to verify before relying on specifics._"
)


def verify_digest(summary, articles, sources, model):
    """Cross-check the generated digest against its source texts.

    A small model will confidently invent specifics, so this runs one extra
    Ollama call asking whether every claim is grounded in the provided source
    text. If unsupported claims are found, a caution note is appended. On any
    LLM/parse failure the digest is returned unchanged — verification must
    never block delivery.
    """
    sources_text = ""
    for i, (a, src) in enumerate(zip(articles, sources), 1):
        sources_text += f"\n---\nSOURCE {i} — {a['title']}\n{src}\n"

    prompt = (
        f"Below is a news digest, followed by the source texts it was written from "
        f"and a profile of the reader (James) it was written for.\n\n"
        f"=== DIGEST ===\n{summary}\n\n"
        f"=== SOURCE TEXTS ==={sources_text}\n\n"
        f"=== READER PROFILE ===\n{JAMES_PROFILE}\n\n"
        f"Flag a claim as unsupported ONLY if EITHER:\n"
        f"(a) it states a news specific (name, number, date, product, quote, action) "
        f"not present in any source text, OR\n"
        f"(b) it makes a POSITIVE false statement about James himself — asserts he "
        f"does/has/studies something that contradicts or is absent from the reader "
        f"profile (e.g. 'James trades crypto', 'James plays guitar').\n"
        f"This is critical — do NOT flag any of the following, they are all fine:\n"
        f"- An article whose topic simply does not relate to James. Unrelated news "
        f"is not a false claim; the digest is allowed to cover it plainly.\n"
        f"- Formatting, or reasonable paraphrase of a source.\n"
        f"- The digest's editorial opinions — the lead line and every 'Worth your "
        f"time?' verdict are the writer's judgment, never factual claims.\n"
        f"Only flag (b) when the text literally asserts a NEW personal fact about "
        f"James that is not true. If unsure, do not flag.\n\n"
        f'Respond with ONLY a JSON object: {{"supported": true}} if everything checks '
        f'out, or {{"supported": false, "issues": ["short description", ...]}} listing '
        f"each unsupported claim. No other text."
    )
    system = (
        "You are a fact-checker comparing a summary against its sources and the "
        "reader's true profile. Be strict about invented specifics and invented "
        "personal facts, but tolerant of paraphrase. Output only valid JSON."
    )

    raw = ask_ollama(prompt, system=system, model=model)
    if not raw:
        logging.warning("Digest verification failed (no LLM response); sending unchecked.")
        return summary

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        logging.warning("Digest verification returned no JSON; sending unchecked.")
        return summary
    try:
        verdict = json.loads(match.group())
    except json.JSONDecodeError:
        logging.warning("Digest verification JSON unparseable; sending unchecked.")
        return summary

    if verdict.get("supported") is False:
        issues = verdict.get("issues", [])
        logging.warning(
            "Digest verification flagged %d unsupported claim(s): %s",
            len(issues), "; ".join(str(i) for i in issues) or "(unspecified)",
        )
        return summary + CAUTION_NOTE

    logging.info("Digest verification passed — all claims supported.")
    return summary


def rank_articles(articles, topics, rules, model, keep=6):
    """Score candidate articles 1-10 for relevance, return the top `keep`.

    Scoring (rate-the-item) plays to a small model's strengths far better than
    prose generation. On any parse/LLM failure this falls back to the original
    order truncated to `keep`, so ranking can never cost us the digest.
    """
    if len(articles) <= keep:
        return articles

    listing = "\n".join(
        f"{i}. [{a['topic']}] {a['title']} — {a['snippet'][:160]}"
        for i, a in enumerate(articles, 1)
    )
    interests = ", ".join(topics)
    rules_note = ("\nReader's standing preferences: " + "; ".join(rules)) if rules else ""
    prompt = (
        f"A reader follows these interests: {interests}.{rules_note}\n\n"
        f"Below are {len(articles)} candidate news items. Rate each one from 1-10 for how "
        f"newsworthy and relevant it is to this reader. Favor concrete developments "
        f"(launches, research results, policy, named companies) over roundups, opinion, "
        f"and hype.\n\n{listing}\n\n"
        f"Respond with ONLY a JSON object mapping each item number (as a string) to its "
        f'integer score, e.g. {{"1": 8, "2": 3}}. No other text.'
    )
    system = (
        "You are a news editor scoring story relevance. Output only valid JSON: "
        "a flat object of item-number strings to integer scores 1-10."
    )

    raw = ask_ollama(prompt, system=system, model=model)
    if not raw:
        logging.warning("Article ranking failed (no LLM response); using fetch order.")
        return articles[:keep]

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        logging.warning("Article ranking returned no JSON; using fetch order.")
        return articles[:keep]
    try:
        scores = json.loads(match.group())
    except json.JSONDecodeError:
        logging.warning("Article ranking JSON unparseable; using fetch order.")
        return articles[:keep]

    def score_of(idx):
        # 1-based item number as the model saw it; default mid-low if missing.
        try:
            return float(scores.get(str(idx + 1), 4))
        except (TypeError, ValueError):
            return 4.0

    ranked = sorted(range(len(articles)), key=score_of, reverse=True)
    top = [articles[i] for i in ranked[:keep]]
    logging.info(
        "Ranked %d candidates -> kept %d. Top scores: %s",
        len(articles), len(top),
        ", ".join(f"{a['title'][:40]}={score_of(articles.index(a)):.0f}" for a in top),
    )
    return top


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
        f"{VOICE}\n\n"
        "Summarize this YouTube video for Telegram (Markdown). "
        "Extract real substance: core ideas, direct quotes, and relevance to the AI world. No fluff. "
        "The warmth is in how you talk to James, never in inventing substance the video lacks. "
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
    _, greeting = time_of_day()
    rules_ctx = ("\n\nExtra instructions:\n" + "\n".join(f"- {r}" for r in rules)) if rules else ""

    sections = [f"*{greeting}, James* 👋\n_{now}_\n{'─' * 28}"]

    # --- News ---
    sections.append("\n*AI NEWS*\n")
    logging.info("Fetching news...")
    history = load_history()
    sent_articles = []
    articles = fetch_news(topics, history=history)
    if articles:
        articles = rank_articles(articles, topics, rules, model, keep=6)
        logging.info(f"Summarizing {len(articles)} articles...")
        news_digest, articles, sources = build_news_digest(articles, model, rules_ctx)
        logging.info("Verifying digest against sources...")
        news_digest = verify_digest(news_digest, articles, sources, model)
        sent_articles = articles
        sections.append(news_digest)
        sections.append("\n*Sources*")
        for i, a in enumerate(articles, 1):
            sections.append(f"{i}. [{a['title']}]({a['url']})")
    else:
        sections.append("_(No new news since the last digest)_")

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

    # Record sent news so future digests skip it. Newest entries first; the
    # rolling cap is enforced in save_history(). Done after send so a failed
    # delivery does not silently suppress those stories next run.
    if sent_articles:
        today = datetime.now().strftime("%Y-%m-%d")
        new_entries = [
            {
                "url_key": a.get("url_key", ""),
                "title_key": a.get("title_key", ""),
                "title": a["title"],
                "date": today,
            }
            for a in sent_articles
        ]
        save_history(new_entries + history)
        logging.info(f"Recorded {len(new_entries)} articles to history.")


if __name__ == "__main__":
    main()
