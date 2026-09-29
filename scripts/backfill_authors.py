#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把定向博主的**存量**笔记一次性收进候选池。

为什么需要它
------------
`harvest()` 的 author 阶段每轮只滚动收集 `author_num_notes` 张卡（默认 60），
拿到的永远是主页**最新的那一批**。也就是说博主的早期笔记永远滚不到，
池子里翻来覆去就那几十张。

新加一个博主、或想把某个博主的全部历史内容纳入选帖范围时，先跑一次这个，
用大得多的 num_notes 把存量一次性灌进 `data/pool/xhs.json`。
之后每轮的 author 阶段只负责"接住新发的"。

用法
----
    # 建议用 browser_guard 包住，否则会弹出 Chrome 窗口
    python scripts\\browser_guard.py --run -- python scripts\\backfill_authors.py
    python scripts\\browser_guard.py --run -- python scripts\\backfill_authors.py --per 300
    python scripts\\browser_guard.py --run -- python scripts\\backfill_authors.py --id <user_id>

只动候选池，不抓正文、不花 LLM 费用。真正的抓取由下一轮 harvest 从池里抽。
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from common import LOG, load_config                      # noqa: E402
import fetch_xhs as fx                                   # noqa: E402


def main():
    # ---- 自我保护：没被 browser_guard 包住就自动重包一层 ----
    # 2026-09-30 教训：直接跑这个脚本，socai 弹的 Chrome 会出现在用户桌面上
    # （Chrome 还会把窗口恢复到上次停靠位置并按「至少 30px 可见」弹回屏幕边）。
    if os.environ.get("BROWSER_GUARD_ACTIVE") != "1":
        guard = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "browser_guard.py")
        cmd = [sys.executable, guard, "--run", "--", sys.executable,
               os.path.abspath(__file__)] + sys.argv[1:]
        LOG.warning("未在 browser_guard 内运行，自动重包一层（否则会弹 Chrome）")
        return subprocess.call(cmd)

    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=200,
                    help="每个博主主页滚动收集多少张卡（默认 200）")
    ap.add_argument("--id", action="append", default=None,
                    help="只回填指定 user id（可重复）；不给则用 config 里的 authors")
    ap.add_argument("--sleep", type=float, default=10.0,
                    help="每个博主之间的间隔秒数（默认 10）")
    a = ap.parse_args()

    cfg = load_config()
    s = cfg["sources"]["xhs_socai"]
    authors = s.get("authors") or []
    if a.id:
        authors = [{"id": i, "name": "(命令行指定)"} for i in a.id]
    if not authors:
        LOG.error("config 的 xhs_socai.authors 是空的，且没给 --id")
        return 1

    pool = fx.load_pool()
    before = len(pool.get("cards") or {})
    LOG.info("回填前池内 %d 张", before)

    for idx, au in enumerate(authors):
        aid = (au or {}).get("id") if isinstance(au, dict) else au
        label = ((au or {}).get("name") if isinstance(au, dict) else "") or aid
        if not aid:
            continue
        LOG.info("=" * 50)
        LOG.info("回填博主：%s", label)
        cards, prof = fx.author_cards(aid, num_notes=a.per, timeout=1800)
        LOG.info("  主页 %s｜粉丝 %s｜笔记 %s｜收集 %d 张卡",
                 prof.get("display_name") or "-", prof.get("followers") or "-",
                 prof.get("note_count") or "-", len(cards))
        if not cards:
            LOG.warning("  没拿到卡片 —— 检查 id 是否为主页 URL 里的 user id")
        added = fx.merge_pool(pool, cards, "author:%s" % aid,
                              origin="author", author_id=aid)
        LOG.info("  新增入池 %d 张", added)
        fx.save_pool(pool)
        if idx < len(authors) - 1:
            time.sleep(a.sleep)

    after = len(pool.get("cards") or {})
    fresh = fx.pool_fresh_cards(pool, fx.load_seen())
    LOG.info("=" * 50)
    LOG.info("回填完成：池内 %d → %d 张，未抓过 %d 张", before, after, len(fresh))
    LOG.info("下一轮 harvest 会从这些卡里按 author_pick 抽正文")
    return 0


if __name__ == "__main__":
    sys.exit(main())
