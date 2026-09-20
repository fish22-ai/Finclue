#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清洗 → 去重 → 过滤。

去重状态存本地（data/seen.jsonl），不依赖飞书查询 —— 与 xhs-teardown
的 analyzed_authors.json 是同一种思路：表里保持干净，内部 ID 不外泄。
"""

import hashlib
import os
import re
import urllib.parse

from common import LOG, LOG as _L, append_jsonl, ensure_dir, load_config, path, read_jsonl, sanitize

_WS = re.compile(r"[ \t\u3000]+")
_NL = re.compile(r"\n{3,}")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def canonical_key(rec):
    """稳定去重键。

    小红书不能拿 URL 当键 —— URL 里的 xsec_token 每次搜索都会变。
    """
    src = rec.get("source") or "?"
    nid = rec.get("note_id") or ""
    if src == "xhs" and nid:
        return "xhs:%s" % nid
    url = rec.get("source_url") or ""
    if url:
        u = urllib.parse.urlparse(url)
        clean = "%s://%s%s" % (u.scheme, u.netloc, u.path.rstrip("/"))
        return "%s:%s" % (src, clean)
    if nid:
        return "%s:%s" % (src, nid)
    return "%s:%s" % (src, hashlib.md5(
        (rec.get("title") or "").encode("utf-8")).hexdigest()[:16])


# --------------------------------------------------------------- 清洗
def build_content(rec):
    """把一条原始记录拼成喂给 LLM 的正文。

    小红书的内容分三块，缺一不可：
      body     —— 正文
      ocr_text —— 逐图 OCR（JD 截图、岗位表格都在这里）
      comments —— 评论（常含一手补充信息）
    """
    parts = []
    title = (rec.get("title") or "").strip()
    if title:
        parts.append("【标题】" + title)
    if rec.get("author"):
        parts.append("【作者】" + str(rec["author"]))
    if rec.get("published_at"):
        parts.append("【发布时间】" + str(rec["published_at"]))
    if rec.get("location"):
        parts.append("【定位】" + str(rec["location"]))

    body = (rec.get("body") or "").strip()
    if body:
        parts.append("\n【正文】\n" + body)

    ocr = (rec.get("ocr_text") or "").strip()
    if ocr:
        parts.append("\n【图片文字（OCR）】\n" + ocr)

    comments = (rec.get("comments") or "").strip()
    if comments:
        parts.append("\n【评论区】\n" + comments)

    if rec.get("hashtags"):
        tags = rec["hashtags"]
        if isinstance(tags, list) and tags:
            parts.append("\n【话题标签】" + " ".join(
                "#" + str(t).lstrip("#") for t in tags))

    text = "\n".join(parts)
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return sanitize(text)


def clean(records):
    out = []
    for r in records:
        content = build_content(r)
        if not content:
            continue
        r = dict(r)
        r["content"] = content
        r["dedup_key"] = canonical_key(r)
        out.append(r)
    LOG.info("清洗：%d 条 → %d 条", len(records), len(out))
    return out


# --------------------------------------------------------------- 去重
SEEN_FILE = "data/seen.jsonl"


def load_seen():
    rows = read_jsonl(path(SEEN_FILE))
    return {r["key"] for r in rows if r.get("key")}


def dedup(records, seen=None):
    if seen is None:
        seen = load_seen()
    fresh, dup = [], 0
    batch_seen = set()
    for r in records:
        k = r["dedup_key"]
        if k in seen or k in batch_seen:
            dup += 1
            continue
        batch_seen.add(k)
        fresh.append(r)
    LOG.info("去重：%d 条 → %d 条（跳过 %d 条已见）",
             len(records), len(fresh), dup)
    return fresh


def mark_seen(records):
    for r in records:
        append_jsonl(path(SEEN_FILE), {
            "key": r["dedup_key"],
            "source": r.get("source"),
            "url": r.get("source_url"),
        })


# --------------------------------------------------------------- 过滤
def filter_records(records, cfg=None):
    cfg = cfg or load_config()
    f = cfg["filter"]
    kw = [k.lower() for k in (f.get("relevance_keywords") or [])]
    inst = [k.lower() for k in (f.get("institution_keywords") or [])]
    block = [k.lower() for k in (f.get("blocklist_keywords") or [])]
    min_len = int(f.get("min_content_length", 120))

    keep, dropped = [], {"too_short": 0, "blocklist": 0, "irrelevant": 0}
    for r in records:
        text = (r.get("content") or "")
        low = text.lower()

        if len(text) < min_len:
            dropped["too_short"] += 1
            continue
        if any(b in low for b in block):
            dropped["blocklist"] += 1
            continue

        # 招聘 JD 类天然含岗位词，放宽：命中机构词或相关性词任一即可
        hit_rel = any(k in low for k in kw)
        hit_inst = any(k in low for k in inst)
        if not (hit_rel or hit_inst):
            dropped["irrelevant"] += 1
            continue
        keep.append(r)

    LOG.info("过滤：%d 条 → %d 条（过短 %d，噪音 %d，不相关 %d）",
             len(records), len(keep), dropped["too_short"],
             dropped["blocklist"], dropped["irrelevant"])
    return keep
