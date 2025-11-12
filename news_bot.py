#!/usr/bin/env python3
import os
import json
import time
import logging
import hashlib
from datetime import datetime
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lex_rank import LexRankSummarizer

from telegram import Bot

# -------------- Настройки (основные через environment / GitHub secrets) --------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
KEYWORDS = os.environ.get("KEYWORDS", "финансовая, платформа, банки").split(",")
SOURCES_FILE = "sources.txt"  # optional: file with RSS/URLs
PROCESSED_FILE = "processed.json"  # state file stored/коммитится в repo

# default RSS sources (можно дополнить)
DEFAULT_RSS = [
    # Google News RSS по поиску (пример)
    "https://news.google.com/rss/search?q=%22регулирование%22+OR+%22законопроект%22&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=%22госдума%22&hl=ru&gl=RU&ceid=RU:ru",
    # правительственные и ведомственные RSS (пример)
    "https://www.garant.ru/rss/news.rss",
    # добавьте свои RSS здесь
]

# --------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news-watch")

bot = Bot(token=TELEGRAM_TOKEN)


def load_sources():
    if os.path.exists(SOURCES_FILE):
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip() and not l.startswith("#")]
            return lines + DEFAULT_RSS
    return DEFAULT_RSS


def load_state():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed": []}


def save_state(state):
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def md5_text(s: str):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def summarize_text(text: str, sentence_count=3):
    # Simple extractive summarizer using sumy (LexRank)
    parser = PlaintextParser.from_string(text, Tokenizer("russian"))
    summarizer = LexRankSummarizer()
    summary = summarizer(parser.document, sentence_count)
    return " ".join([str(s) for s in summary])


def fetch_rss(url):
    try:
        feed = feedparser.parse(url)
        entries = []
        for e in feed.entries:
            title = e.get("title", "")
            link = e.get("link", "")
            published = e.get("published", "") or e.get("updated", "")
            summary = e.get("summary", "")
            content = summary
            entries.append({"title": title, "link": link, "published": published, "summary": content})
        return entries
    except Exception as ex:
        logger.exception("RSS fetch failed for %s: %s", url, ex)
        return []


def fetch_plain_article_text(url, max_chars=4000):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "news-watch-bot/1.0"})
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "lxml")
        # попытка собрать текст из параграфов
        paragraphs = soup.find_all("p")
        text = "\n".join(p.get_text().strip() for p in paragraphs)
        if not text:
            # как fallback — взять весь текст страницы
            text = soup.get_text()
        return text[:max_chars]
    except Exception as ex:
        logger.exception("Article fetch failed %s", url)
        return ""


def matches_keywords(text, keywords):
    text_low = text.lower()
    for k in keywords:
        k = k.strip().lower()
        if not k:
            continue
        if k in text_low:
            return True
    return False


def make_message(item, summary):
    title = item.get("title", "").strip()
    link = item.get("link", "").strip()
    published = item.get("published", "")
    published_str = published if published else datetime.utcnow().isoformat()
    msg = f"📰 <b>{title}</b>\n\n"
    msg += f"📅 {published_str}\n"
    msg += f"🔗 {link}\n\n"
    msg += f"📄 <b>Кратко:</b>\n{summary}\n\n"
    return msg


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set.")
        return

    keywords = KEYWORDS
    logger.info("Keywords: %s", keywords)
    sources = load_sources()
    logger.info("Sources count: %d", len(sources))

    state = load_state()
    processed = set(state.get("processed", []))
    new_processed = set()

    to_notify = []

    for src in sources:
        logger.info("Fetching: %s", src)
        if src.startswith("http"):
            # assume RSS (feedparser will try to parse)
            entries = fetch_rss(src)
            # if no entries, maybe it's a plain page with list of links — try to scrape links
            if not entries:
                # try to scrape links from page
                try:
                    r = requests.get(src, timeout=12, headers={"User-Agent": "news-watch-bot/1.0"})
                    soup = BeautifulSoup(r.text, "lxml")
                    links = []
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if href.startswith("http"):
                            links.append({"title": a.get_text(strip=True), "link": href, "published": ""})
                    entries = links[:30]
                except Exception:
                    entries = []
        else:
            entries = []

        for e in entries:
            title = e.get("title", "") or ""
            link = e.get("link", "") or ""
            snippet = e.get("summary", "") or ""
            # id generation
            uid = md5_text((title + link)[:500])
            if uid in processed:
                continue

            # check keywords in title/snippet
            check_text = (title + " " + snippet).lower()
            if not matches_keywords(check_text, keywords):
                # if not in title/snippet, try fetching article text
                article_text = fetch_plain_article_text(link)
                if not matches_keywords(article_text, keywords):
                    continue
            else:
                article_text = fetch_plain_article_text(link)

            # summarize
            if article_text:
                try:
                    summary = summarize_text(article_text, sentence_count=3)
                except Exception:
                    # fallback — take first 3 sentences naively
                    summary = ". ".join(article_text.split(".")[:3]) + "..."
            else:
                summary = (snippet[:300] + "...") if snippet else "Короткого текста нет."

            msg = make_message(e, summary)
            to_notify.append((uid, msg))
            new_processed.add(uid)
            # keep rate friendly
            time.sleep(0.5)

    # send notifications (one by one)
    for uid, message in to_notify:
        try:
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="HTML", disable_web_page_preview=False)
            logger.info("Sent: %s", uid)
            # small pause to avoid Telegram flood
            time.sleep(1)
        except Exception as ex:
            logger.exception("Failed to send message: %s", ex)

    # update processed list and save
    combined = list(processed.union(new_processed))
    state["processed"] = combined[-2000:]  # keep last 2000 ids
    save_state(state)
    logger.info("Run finished. New items: %d", len(new_processed))


if __name__ == "__main__":
    main()
