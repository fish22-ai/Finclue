#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Career Intelligence —— 主入口。

用法：
    python src/run.py --stage fetch --source xhs        # 只抓小红书
    python src/run.py --stage fetch --source all
    python src/run.py --stage extract --limit 3         # 只抽 3 条试跑
    python src/run.py --stage write --dry-run
    python src/run.py --stage insight
    python src/run.py --stage render                    # 只重渲站点（幂等，可随时跑）
    python src/run.py --stage all                       # 完整闭环

八段流水线（fetch/process/extract/write/insight/write_insights/render），
段间用本地 JSON 解耦，每段可独立重跑。

数据落盘与消费方（改这里之前先认清谁是「无条件落盘」的）：
    data/facts/<date>.json          ← 抽取段写，**无条件**。洞察窗口的输入。
    data/pending/<name>_<date>.json ← 推飞书之前写，**无条件**。站点读它拿洞察。
    data/written/<name>_<date>.json ← 推送**成功后**才写。飞书一关就停更，别当数据源。
"""

import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (LOG, ensure_dir, load_config, path, read_json, today,  # noqa: E402
                    write_json)
import extract as extract_mod  # noqa: E402
import insight as insight_mod  # noqa: E402
import pipeline  # noqa: E402
import writer  # noqa: E402

FETCHERS = {}
ALL_SOURCES = ["xhs", "reddit", "linkedin"]


def _fetchers():
    if FETCHERS:
        return FETCHERS
    from fetch_linkedin import harvest as h_li
    from fetch_reddit import harvest as h_rd
    from fetch_xhs import harvest as h_xhs
    FETCHERS.update({"xhs": h_xhs, "reddit": h_rd, "linkedin": h_li})
    return FETCHERS


def enabled_sources():
    """只返回 config 里 enabled: true 的源。"""
    cfg = load_config()["sources"]
    key = {"xhs": "xhs_socai", "reddit": "reddit_rss",
           "linkedin": "linkedin_jobs"}
    out = [s for s in ALL_SOURCES
           if (cfg.get(key[s]) or {}).get("enabled")]
    return out or ["xhs"]


def raw_path(date_str, source):
    return path("data", "raw", "%s_%s.json" % (date_str, source))


def clean_path(date_str):
    return path("data", "clean", "%s.json" % date_str)


# --------------------------------------------------------------- 各段
def stage_fetch(sources, date_str):
    out = {}
    for s in sources:
        fn = _fetchers().get(s)
        if not fn:
            LOG.warning("未知源：%s", s)
            continue
        LOG.info("=" * 50)
        LOG.info("抓取：%s", s)
        try:
            recs = fn()
        except Exception as e:
            LOG.error("抓取 %s 失败：%r", s, e)
            recs = []
        write_json(raw_path(date_str, s), recs)
        LOG.info("→ %s 抓取 %d 条", s, len(recs))
        out[s] = len(recs)
    return out


def load_raw(date_str, sources=None):
    base = path("data", "raw")
    if not os.path.isdir(base):
        return []
    recs = []
    for fn in sorted(os.listdir(base)):
        if not fn.startswith(date_str) or not fn.endswith(".json"):
            continue
        if sources:
            if not any(fn == "%s_%s.json" % (date_str, s) for s in sources):
                continue
        data = read_json(os.path.join(base, fn), []) or []
        recs.extend(data)
    return recs


def stage_process(date_str):
    raw = load_raw(date_str)
    LOG.info("=" * 50)
    LOG.info("处理：读入 %d 条原始记录", len(raw))
    recs = pipeline.clean(raw)
    recs = pipeline.dedup(recs)
    recs = pipeline.filter_records(recs)
    write_json(clean_path(date_str), recs)
    LOG.info("→ %s", clean_path(date_str))
    return recs


def stage_extract(records, date_str, limit=None, dry_run=False, force=False):
    LOG.info("=" * 50)
    LOG.info("抽取：%d 条待处理%s%s", len(records),
             "（限 %d 条）" % limit if limit else "",
             "（--force 绕过缓存）" if force else "")
    if dry_run:
        LOG.info("[dry] 跳过 LLM 调用")
        return []
    facts, stats = extract_mod.extract_all(records, date_str, limit, force)
    # ⚠️ extract_all 在 records 为空时返回 []，而这里原来是无条件覆盖。
    #    同一天重跑（daily.bat force / --stage process）时，所有笔记都已在
    #    seen.jsonl 里 → records 必为空 → 当天事实被清成 []。
    #    以前只伤到 --stage write，现在 data/facts/ 同时是洞察窗口（insight.read_facts）
    #    和站点的数据源，覆盖就等于抹掉一天。空结果不许动已有文件。
    p = path("data", "facts", "%s.json" % date_str)
    if facts or not os.path.exists(p):
        write_json(p, facts)
    else:
        LOG.warning("抽取结果为空，保留已有 %s（不覆盖）", p)
    return facts


def stage_write_facts(facts, date_str, dry_run=False):
    LOG.info("=" * 50)
    if not facts:
        LOG.info("没有事实要写入")
        return 0
    writer.save_local(facts, date_str, "facts")
    return writer.write_records(facts, "facts", date_str, dry_run)


def stage_insight(dry_run=False):
    LOG.info("=" * 50)
    if dry_run:
        LOG.info("[dry] 跳过洞察生成")
        return []
    return insight_mod.generate()


def stage_render():
    """渲染本地阅读端 site/（2026-09-18 起这是主要阅读入口）。

    ⚠️ 失败判为**非致命**（记 ERROR 后返回 0）。渲染不改任何数据，重跑也不丢东西；
    做成致命反而更糟 —— daily.bat 的哨兵只挡「当天」，而任务 3 天一次，
    失败那天照样得等下次运行才被补上，白白多一条误导性的 cron.log
    （那边的失败原因清单全是 socai / LLM / 候选池，会把人带偏）。
    自愈靠 render_all() 每次都重渲所有期。
    """
    import render as render_mod
    try:
        n, n_facts, n_ins = render_mod.render_all()
        if n == 0:
            LOG.warning("渲染：没有可渲染的数据，site/ 未更新")
        return n
    except Exception as e:
        LOG.error("渲染失败（数据无损，下次运行会重渲所有期）：%r", e)
        LOG.error(traceback.format_exc())
        return 0


def stage_write_insights(items, date_str, dry_run=False):
    if not items:
        LOG.info("没有洞察要写入")
        return 0
    writer.save_local(items, date_str, "insights")
    return writer.write_records(items, "insights", date_str, dry_run)


# --------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    # ⚠️ 往这里加阶段，必须同时在下面就同样的名字加一条 if 分支。
                    #    只加 choices 不加分支的话，该阶段会落进下面的 `else: records = []`，
                    #    跳过所有 if、打成「完成」并返回 0 —— 一个静默空操作。
                    choices=["all", "fetch", "process", "extract", "write",
                             "insight", "render"])
    ap.add_argument("--source", default="all",
                    help="all（只取 config 里 enabled 的源）或逗号分隔：xhs,reddit,linkedin")
    ap.add_argument("--limit", type=int, default=None,
                    help="抽取阶段最多处理几条（试跑用）")
    ap.add_argument("--date", default=None, help="覆盖日期 YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="强制重抽：绕过 seen 去重与逐条抽取缓存（重跑同一天时用）")
    a = ap.parse_args()

    date_str = a.date or today()
    sources = (enabled_sources() if a.source == "all"
               else [s.strip() for s in a.source.split(",") if s.strip()])

    ensure_dir(path("data"))
    LOG.info("Career Intelligence | 日期 %s | 阶段 %s | 源 %s",
             date_str, a.stage, ",".join(sources))

    try:
        if a.stage in ("all", "fetch"):
            counts = stage_fetch(sources, date_str)
            # 无人值守时最怕「静默成功但产出为空」：socai 登录态失效时，
            # get-notes 会逐条失败但只是 warning，整轮仍会以 0 条「成功」结束，
            # 定时任务看不出任何异常。这里显式失败，让 cron.log 有迹可循。
            if not any(counts.values()):
                LOG.error("抓取结果为 0 条 —— 判为失败，不继续往下跑。常见原因：")
                LOG.error("  1) socai 登录态失效（需人工重新扫码登录）")
                LOG.error("  2) 搜索全部失败，且候选池已空")
                LOG.error("  详见 data/logs/%s.log 里的搜索/取正文记录", date_str)
                return 1

        if a.stage == "fetch":
            LOG.info("完成（仅抓取）")
            return 0

        if a.stage in ("all", "process"):
            records = stage_process(date_str)
        elif a.stage in ("extract", "write"):
            # ⚠️ 这条分支原来直接读 clean/*.json，完全绕过 dedup 与 seen.jsonl ——
            #    重跑会把已经抽过的笔记再抽一遍（2026-09-15 同一篇被抽了 3 次）。
            records = read_json(clean_path(date_str), []) or []
            if records and not a.force:
                records = pipeline.dedup(records)
            if not records:
                LOG.warning("没有可处理的 clean 数据（没跑过 fetch，或全在 seen 里）；"
                            "确实要重抽请加 --force")
        else:
            records = []

        if a.stage in ("all", "process"):
            facts = stage_extract(records, date_str, a.limit, a.dry_run, a.force)
            stage_write_facts(facts, date_str, a.dry_run)
            pipeline.mark_seen(records)

        elif a.stage == "extract":
            facts = stage_extract(records, date_str, a.limit, a.dry_run, a.force)
            LOG.info("抽取结果已落 %s", path("data", "facts", "%s.json" % date_str))

        elif a.stage == "write":
            facts = read_json(path("data", "facts", "%s.json" % date_str), [])
            if not facts:
                LOG.warning("没有抽取结果，先跑 --stage extract")
            stage_write_facts(facts, date_str, a.dry_run)
            # 只有成功写入才标记 seen，避免抽取失败的数据被永久跳过
            if facts and not a.dry_run:
                pipeline.mark_seen(records)

        if a.stage in ("all", "insight"):
            items = stage_insight(a.dry_run)
            stage_write_insights(items, date_str, a.dry_run)

        # 渲染放在最后：--stage all 必须渲「这一轮刚生成」的数据
        if a.stage in ("all", "render"):
            stage_render()

        # 成本报告 —— 让每次跑完都能看到 token 花在哪
        try:
            import llm as llm_mod
            llm_mod.report_usage()
        except Exception:
            pass

        LOG.info("=" * 50)
        LOG.info("完成")
        return 0

    except KeyboardInterrupt:
        LOG.warning("用户中断")
        return 130
    except Exception as e:
        LOG.error("运行失败：%r", e)
        LOG.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
