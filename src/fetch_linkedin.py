#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LinkedIn 公开岗位采集 —— 走 jobs-guest API，无需登录。

返回结构化卡片：职位 / 公司 / 地点 / 发布时间 / 详情 URL
—— 唯一能稳定提供 company+role+city+published_at 四要素的源。
"""

import re
import time
import urllib.error
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup

from common import LOG, load_config, sanitize_deep

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

API = ("https://www.linkedin.com/jobs-guest/jobs/api/"
       "seeMoreJobPostings/search?keywords={kw}&location={loc}&start={start}")
DETAIL_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"


def _get(url, timeout=25):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml")
    req.add_header("Accept-Language", "en-US,en;q=0.9,zh-CN;q=0.8")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _text(node):
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def parse_cards(html_text):
    """解析 <li> 卡片。选择器取自 LinkedIn 公开页的稳定类名。"""
    soup = BeautifulSoup(html_text, "lxml")
    out = []
    for li in soup.find_all("li"):
        t = li.select_one(".base-search-card__title")
        if not t:
            continue
        co = li.select_one(".hidden-nested-link") or \
            li.select_one(".base-search-card__subtitle")
        loc = li.select_one(".job-search-card__location")
        a = li.select_one("a.base-card__full-link") or li.find("a", href=True)
        dt = li.find("time")
        out.append({
            "title": _text(t),
            "company": _text(co),
            "location": _text(loc),
            "url": (a.get("href", "").split("?")[0] if a else ""),
            "published_at": (dt.get("datetime") if dt else "") or "",
        })
    return out


def job_id_from_url(url):
    """从详情 URL 末尾取 jobId。形如 .../view/xxx-at-hsbc-4461565475"""
    tail = (url or "").rstrip("/").split("/")[-1]
    m = re.search(r"(\d{6,})$", tail)
    return m.group(1) if m else None


def fetch_detail(job_id, timeout=25):
    """拉单个岗位的完整 JD 正文（公开接口，无需登录）。

    ⚠️ 卡片本身只有 标题/公司/地点，没有 JD 正文 —— 不拉详情就填不了
       recruiting_bar / education_requirement 这些字段。
    """
    try:
        html_text = _get(DETAIL_API.format(jid=job_id), timeout)
    except Exception as e:
        LOG.warning("    详情拉取失败 %s：%r", job_id, e)
        return ""
    soup = BeautifulSoup(html_text, "lxml")
    node = (soup.select_one(".show-more-less-html__markup")
            or soup.select_one(".description__text"))
    if not node:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def harvest(source_cfg=None):
    cfg = load_config()
    s = source_cfg or cfg["sources"]["linkedin_jobs"]
    queries = s.get("queries") or []
    pages = int(s.get("pages_per_query", 2))
    sleep_s = float(s.get("rate_limit_seconds", 3))
    with_details = bool(s.get("fetch_details", True))
    max_details = int(s.get("max_details", 30))

    records, seen = [], set()
    for q in queries:
        kw = urllib.parse.quote(q["keywords"])
        loc = urllib.parse.quote(q["location"])
        for page in range(pages):
            url = API.format(kw=kw, loc=loc, start=page * 10)
            LOG.info("LinkedIn 拉取：%s @ %s (page %d)",
                     q["keywords"], q["location"], page + 1)
            try:
                html_text = _get(url)
            except urllib.error.HTTPError as e:
                LOG.warning("  HTTP %s", e.code)
                break
            except Exception as e:
                LOG.warning("  失败：%r", e)
                break

            cards = parse_cards(html_text)
            LOG.info("  %d 张卡片", len(cards))
            for c in cards:
                u = c.get("url") or ""
                if not u or u in seen:
                    continue
                seen.add(u)
                records.append({
                    "source": "linkedin",
                    "source_url": u,
                    "note_id": u,
                    "title": c["title"],
                    "author": c["company"],          # 公司即"作者"
                    "published_at": c["published_at"],
                    "likes": 0,
                    "body": "%s\n%s\n%s" % (c["title"], c["company"], c["location"]),
                    "ocr_text": "",
                    "comments": "",
                    "hashtags": [],
                    "company": c["company"],
                    "location": c["location"],
                    "role": c["title"],
                    "has_detail": False,
                })
            time.sleep(sleep_s)

    LOG.info("LinkedIn 卡片共 %d 条（去重后）", len(records))

    # ---- 拉 JD 正文：只对"可能相关"的前 max_details 条拉，控制请求量
    if with_details and records:
        import pipeline as _pl  # 局部导入，避免循环依赖
        scored = []
        for r in records:
            text = ("%s %s" % (r.get("title", ""), r.get("company", ""))).lower()
            scored.append((not _looks_irrelevant(text), r))
        scored.sort(key=lambda x: not x[0])
        targets = [r for ok, r in scored if ok][:max_details]

        LOG.info("拉取 JD 正文：%d 条", len(targets))
        got = 0
        for i, r in enumerate(targets, 1):
            jid = job_id_from_url(r["source_url"])
            if not jid:
                continue
            detail = fetch_detail(jid)
            if detail:
                r["body"] = "%s\n%s\n%s\n\n%s" % (
                    r["title"], r["company"], r["location"], detail)
                r["has_detail"] = True
                got += 1
            if i % 5 == 0:
                LOG.info("  进度 %d/%d，已拿到 %d 篇正文", i, len(targets), got)
            time.sleep(sleep_s)
        LOG.info("JD 正文：%d/%d 条成功", got, len(targets))

    return sanitize_deep(records)


# 明显不相关的岗位词（资深岗 / 非目标方向），不浪费请求去拉详情
_IRRELEVANT = re.compile(
    r"\b(vice president|vp|director|managing director|head of|"
    r"chief|president|partner|principal|senior manager)\b", re.I)


def _looks_irrelevant(text):
    return bool(_IRRELEVANT.search(text))
