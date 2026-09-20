#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Reddit 采集 —— 走 RSS 端点。

⚠️ Reddit 的 JSON API（/new.json、/search.json）返回 403，但 RSS 端点可用。
   不要因为 JSON 被封就改成别的路子。
⚠️ 连续请求会 429，间隔需 ≥5 秒。
"""

import html
import re
import time
import urllib.error
import urllib.request

import feedparser

from common import LOG, load_config, sanitize_deep

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")


def _strip_html(s):
    if not s:
        return ""
    s = s.replace("<br/>", "\n").replace("<br>", "\n")
    s = s.replace("</p>", "\n\n")
    s = _TAG.sub("", s)
    s = html.unescape(s)
    # Reddit RSS 顶部有 <!-- SC_OFF --> 之类的注释残留
    s = s.replace("SC_OFF", "").replace("SC_ON", "")
    s = _WS.sub(" ", s)
    s = _NL.sub("\n\n", s)
    return s.strip()


def fetch_sub(sub, limit=25, timeout=25, retries=2):
    """拉单个 subreddit 的 RSS。429 时退避重试。"""
    url = "https://www.reddit.com/r/%s/.rss" % sub
    delay = 20
    for attempt in range(retries + 1):
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/rss+xml, application/xml, text/xml")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return feedparser.parse(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                LOG.info("  429 限流，%ss 后重试（%d/%d）", delay, attempt + 1, retries)
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _extract_link(entry):
    """feedparser 对 Atom 的 link 处理有时给 dict/list。"""
    links = entry.get("links") or []
    for l in links:
        if l.get("rel") == "alternate" and l.get("href"):
            return l["href"]
    if links:
        return links[0].get("href", "")
    return entry.get("link", "")


def _published(entry):
    for k in ("updated", "published"):
        v = entry.get(k)
        if v:
            return v[:10]
    return ""


def harvest(source_cfg=None):
    """Reddit 采集主流程。返回 canonical record 列表。"""
    cfg = load_config()
    s = source_cfg or cfg["sources"]["reddit_rss"]
    subs = s.get("subs") or []
    per = int(s.get("max_items_per_run", 25))
    sleep_s = float(s.get("rate_limit_seconds", 5))

    records = []
    for sub in subs:
        LOG.info("Reddit 拉取：r/%s", sub)
        try:
            feed = fetch_sub(sub, per)
        except urllib.error.HTTPError as e:
            LOG.warning("  HTTP %s —— 若是 429 说明请求过快；若是 403 说明端点不对",
                        e.code)
            time.sleep(sleep_s)
            continue
        except Exception as e:
            LOG.warning("  失败：%r", e)
            time.sleep(sleep_s)
            continue

        entries = (feed.get("entries") or [])[:per]
        LOG.info("  取到 %d 条", len(entries))
        for e in entries:
            body = ""
            if e.get("content"):
                body = _strip_html(e["content"][0].get("value", ""))
            elif e.get("summary"):
                body = _strip_html(e["summary"])

            records.append({
                "source": "reddit",
                "source_url": _extract_link(e),
                "note_id": e.get("id", ""),
                "title": _strip_html(e.get("title", "")),
                "author": (e.get("author") or "").replace("/u/", ""),
                "published_at": _published(e),
                "likes": 0,          # RSS 不含赞数
                "body": body,
                "ocr_text": "",
                "comments": "",
                "hashtags": [t.get("term") for t in (e.get("tags") or [])
                             if t.get("term")],
                "subreddit": sub,
            })
        time.sleep(sleep_s)

    return sanitize_deep(records)
