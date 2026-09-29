#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「小红书号 / 昵称」解析成 socai 可用的 author_id（24 位 hex）。

背景：config 的 authors[].id 必须是主页 URL /user/profile/<id> 的尾段。
直接拿「小红书号」请求 author 会返回 ok=true 但 profile 全空的假成功
（2026-09-29 实测，eco413 / 49628490145 都踩过）。

解析办法（自动化了 2026-09-29 的手工流程）：
  1) 拿 handle 当搜索词 search --preview，数结果卡里的 author_id；
  2) 对出现次数最多的前 3 个候选逐个开主页 --preview 验证 —— profile
     header 里有「小红书号 / 昵称 / bio」，任一字段命中 handle 即认领。
结果落 data/author_resolve.json 供人复核，最终仍需人工确认名字没认错人。

用法：
    python scripts/resolve_authors.py                 # 解析 HANDLE 里的全部
    python scripts/resolve_authors.py 8939926004 ...  # 只解析指定的
输出：stdout 每个 handle 一段结论；data/author_resolve.json 全量明细。
"""

import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from fetch_xhs import author_cards, search_cards  # noqa: E402
from common import path, write_json  # noqa: E402

# 2026-09-30 用户提供的新一批账号（顺序：guanchayuan7 / CFA笔记收集 / 3 个纯号码 / 昵称号）
HANDLES = [
    "guanchayuan7",
    "8939926004",
    "1060654173",
    "63804271801",
    "600805759",
    "日月有双",
]

SLEEP_BETWEEN = 8      # socai 轻量调用间隔，别贴着风控跑
TOP_CANDIDATES = 3     # 每个 handle 最多验证几个候选 author_id


def norm(s):
    return re.sub(r"\s+", "", str(s or "")).lower()


def profile_hits(prof, handle):
    """profile 的任何字符串字段（含嵌套）命中 handle 就算认领。"""
    h = norm(handle)

    def walk(o):
        if isinstance(o, dict):
            return any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        if isinstance(o, str):
            return h in norm(o)
        return False

    return walk(prof)


def resolve(handle):
    """返回 (author_id | None, 明细 dict)。"""
    detail = {"handle": handle, "candidates": [], "resolved": None}
    cards = search_cards(handle, num_notes=20, filters=None, timeout=300)
    detail["search_cards"] = len(cards)

    tally = {}
    for c in cards:
        aid = c.get("author_id")
        if not aid:
            continue
        e = tally.setdefault(aid, {"name": c.get("author") or "", "n": 0})
        e["n"] += 1
        if not e["name"] and c.get("author"):
            e["name"] = c["author"]

    ranked = sorted(tally.items(), key=lambda kv: -kv[1]["n"])
    detail["candidates"] = [{"author_id": a, "name": v["name"], "hits": v["n"]}
                            for a, v in ranked[:TOP_CANDIDATES]]
    print("  候选：%s" % ", ".join("%s(%s, %d 次)" % (v["name"], a[:8], v["n"])
                                    for a, v in ranked[:TOP_CANDIDATES]) or "无")

    for a, info in ranked[:TOP_CANDIDATES]:
        time.sleep(SLEEP_BETWEEN)
        try:
            _cards, prof = author_cards(a, 3)
        except RuntimeError:
            raise  # 登录失效，让外层直接终止
        header = {k: prof.get(k) for k in
                  ("display_name", "nick_name", "xhs_id", "red_id",
                   "ip_location", "followers", "desc", "bio") if prof.get(k)}
        print("  验证 %s… -> %s" % (a[:10], json.dumps(header, ensure_ascii=False)))
        if profile_hits(prof, handle) or norm(info["name"]) == norm(handle):
            detail["resolved"] = a
            detail["profile_header"] = header
            print("  ✔ 认领：%s -> %s" % (handle, a))
            return a, detail
        time.sleep(SLEEP_BETWEEN)

    print("  ✘ %s：前 %d 个候选都没命中，需要人工处理" % (handle, TOP_CANDIDATES))
    return None, detail


def main():
    handles = sys.argv[1:] or HANDLES
    out_file = path("data", "author_resolve.json")
    prev = {}
    if os.path.isfile(out_file):
        with open(out_file, encoding="utf-8") as f:
            prev = {d.get("handle"): d for d in (json.load(f) or [])}

    results = []
    for h in handles:
        if h in prev and prev[h].get("resolved"):
            print("[%s] 已解析过：%s（跳过）" % (h, prev[h]["resolved"]))
            results.append(prev[h])
            continue
        print("[%s] 解析中…" % h)
        try:
            aid, detail = resolve(h)
        except RuntimeError as e:
            print("登录态失效，终止：%r" % (e,))
            break
        results.append(detail)
        time.sleep(SLEEP_BETWEEN)

    write_json(out_file, results)
    print("\n==== 汇总 ====")
    for d in results:
        print("%-14s -> %s" % (d.get("handle"), d.get("resolved") or "未解析"))
    print("明细：%s" % out_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
