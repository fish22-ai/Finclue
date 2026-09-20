#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""洞察生成：读近 N 天事实 → LLM 六主题分析 → 写分析表。

样本量硬约束（同时写进了 prompt，这里做二次校验）：
  0     → 数据不足
  1-2   → 只能 fact
  3-5   → fact + pattern
  >=6   → 完整四层
"""

import io
import json
import re
import urllib.parse

from common import (LOG, load_config, local_dated_files, path, read_json,  # noqa: E402
                    today)
from llm import LLM
from schema import FINDING_LAYERS, INSIGHTS_KEYS, TOPICS

PROMPT_FILE = path("prompts", "generate_insights.md")


def load_prompt():
    """从 prompts/generate_insights.md 抽 SYSTEM 与 USER 模板。

    ⚠️ SYSTEM 必须从 `## SYSTEM` 一直取到 `## USER 模板` 之前 ——
       中间夹着 `## 分析主题`、`## 输出 JSON Schema` 等小节。
       早先写成「取到下一个 ## 为止」，会把这些整段丢掉（同 extract.py 的坑）。
    """
    with io.open(PROMPT_FILE, encoding="utf-8") as f:
        md = f.read()

    m = re.search(r"^##\s*USER\s*模板\s*$", md, re.M)
    if not m:
        raise RuntimeError("prompts/generate_insights.md 里找不到 `## USER 模板` 段")
    user_start = m.start()

    m = re.search(r"^##\s*SYSTEM\s*$", md, re.M)
    if not m:
        raise RuntimeError("prompts/generate_insights.md 里找不到 `## SYSTEM` 段")
    system = md[m.end():user_start].strip()
    system = re.sub(r"\n-{3,}\s*$", "", system).strip()

    tpl = md[user_start:].split("\n", 1)[1].strip()
    tpl = re.sub(r"^```[a-z]*\s*|\s*```$", "", tpl, flags=re.M).strip()
    return system, tpl


# --------------------------------------------------------------- 读事实
def _flat_value(v):
    """字段值归一成字符串。本地 JSON 与飞书字段值形态一致（都是 list/dict/标量）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict):
                parts.append(x.get("text") or x.get("name") or x.get("link") or "")
            else:
                parts.append(str(x))
        return " ".join(p for p in parts if p)
    if isinstance(v, dict):
        return v.get("text") or v.get("link") or ""
    return str(v)


FACT_KEYS_FOR_LLM = [
    "date", "company", "institution_type", "department", "role", "city",
    "salary", "wlb", "recruiting_bar", "actual_bar", "experience_summary",
    "source_type", "tags",
]


def _url_key(u):
    """URL 归一化成去 query 的键。xsec_token 每次搜索都在变，不能当键。"""
    if not u:
        return ""
    p = urllib.parse.urlparse(u)
    return "%s://%s%s" % (p.scheme, p.netloc, p.path.rstrip("/")) or u


def read_facts(window_days):
    """读本地 data/facts/*.json 近 window_days 天的记录，转成精简 dict 列表。

    2026-09-18：数据源从「飞书事实表」改为「本地落盘」。飞书因此降级成可选镜像
    —— 关掉 feishu.write.enabled 后窗口照常累积，两边不再互相依赖。
    （以前这里走 fs.list_records()，一旦停写飞书，~30 天后窗口被饿空，
      六个主题会静默全变成「数据不足」。）

    返回顺序是**新 → 旧**：调用方按顺序装填 prompt 预算，顺序反了会先丢最新的事实。
    """
    import datetime
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=window_days)).isoformat()

    raw = []
    for d, p in local_dated_files("facts"):
        if d < cutoff:
            continue
        for f in (read_json(p, []) or []):
            if isinstance(f, dict):
                raw.append((d, f))
    LOG.info("本地事实：窗口 %d 天（cutoff %s），读到 %d 条",
             window_days, cutoff, len(raw))

    # 跨天去重：--force 跨天重抽会让同一条笔记落在两个日期的文件里，保留最新的那条。
    # 不去重会喂给模型重复样本，把 n 抬高 —— 而 n 决定层级阈值与幻觉守卫。
    seen, out = set(), []
    for d, f in sorted(raw, key=lambda x: x[0], reverse=True):
        key = _url_key(_flat_value(f.get("source_url")))
        if key:
            if key in seen:
                LOG.info("跨天重复，保留最新一条：%s", key[-24:])
                continue
            seen.add(key)
        rec = {k: _flat_value(f.get(k)) for k in FACT_KEYS_FOR_LLM}
        rec["_date"] = d
        out.append(rec)
    return out


FACTS_PROMPT_BUDGET = 60000


def fit_json(records, budget=FACTS_PROMPT_BUDGET):
    """按条装填，超预算就截断 —— 保证交给模型的是**合法 JSON**。

    旧写法 json.dumps(records)[:60000] 有两个毛病：
      1. 从中间劈开一个对象，模型收到残缺 JSON；
      2. 原来的顺序下留下的是**最旧**的几十条，最新的反被丢掉。
    13 条 ≈ 18k 字符，所以 ~42 条就顶到预算；30 天窗口约 130 条，早就溢出了。

    返回 (json_str, 实际条数)。条数必须回传给调用方当 n —— 否则 {{new_count}}
    和 validate_insight 的幻觉守卫会按「没喂进去的条数」来校准。
    """
    kept = []
    for rec in records:
        kept.append(rec)
        if len(json.dumps(kept, ensure_ascii=False, indent=1)) > budget:
            kept.pop()
            break
    if len(kept) < len(records):
        LOG.warning("窗口 %d 条超出 %d 字符预算，只喂最新 %d 条",
                    len(records), budget, len(kept))
    return json.dumps(kept, ensure_ascii=False, indent=1), len(kept)


FACTS_PROMPT_BUDGET = 60000
LAYER_LIMIT = {"fact": 0, "pattern": 3, "interpretation": 3, "implication": 6}


def validate_insight(item, n_facts):
    notes = []
    out = {k: item.get(k) for k in INSIGHTS_KEYS if k in item}

    topic = out.get("topic")
    if topic not in TOPICS:
        low = str(topic or "").strip().lower()
        topic = next((t for t in TOPICS if t.lower() == low), None)
    out["topic"] = topic

    if out.get("finding") in (None, ""):
        out["finding"] = "数据不足"

    # evidence_count 不能超过输入事实数（幻觉检测）
    try:
        ec = int(out.get("evidence_count") or 0)
    except (TypeError, ValueError):
        ec = 0
    if ec > n_facts:
        notes.append("evidence_count=%d 超过输入事实数 %d，判为幻觉，置 0"
                     % (ec, n_facts))
        ec = 0
    out["evidence_count"] = ec

    # 层级与样本量约束
    layer = out.get("finding_layer")
    if layer not in FINDING_LAYERS:
        low = str(layer or "").strip().lower()
        layer = next((l for l in FINDING_LAYERS if l.lower() == low), None)
    if out.get("finding") in ("数据不足", "无增量变化"):
        layer = None
    elif layer and ec < LAYER_LIMIT[layer]:
        notes.append("样本 %d 条不足以支撑 %s 层，降级为 fact" % (ec, layer))
        layer = "fact" if ec >= 1 else None
    # 反推：样本 >=6 但只给了 fact，不改（保守无害）
    out["finding_layer"] = layer

    if out.get("confidence") not in ("high", "medium", "low"):
        out["confidence"] = "low"
    if not out.get("confidence"):
        out["confidence"] = "low"

    cases = out.get("related_cases")
    if isinstance(cases, list):
        out["related_cases"] = "，".join(str(c) for c in cases)
    return out, notes


# --------------------------------------------------------------- 主流程
def generate(window_days=None, dry_run=False):
    cfg = load_config()
    icfg = cfg["insight"]
    window_days = window_days or int(icfg.get("window_days", 30))
    # 二次校验用的阈值（覆盖 prompt 里的默认值）
    min_full = int(icfg.get("min_facts_for_full", 6))

    facts = read_facts(window_days)
    n_window = len(facts)
    LOG.info("洞察分析：窗口 %d 天，%d 条事实", window_days, n_window)

    if n_window == 0:
        LOG.warning("窗口内没有事实数据 —— 所有主题将写入「数据不足」")

    # n 取「真正喂进 prompt 的条数」，不是窗口里的总条数：超预算截断时两者不等，
    # 而 n 要用来校准 validate_insight 的幻觉守卫与层级阈值。
    facts_json, n = fit_json(facts)
    LOG.info("洞察分析：窗口 %d 天，%d 条事实", window_days, n_window)

    if n_window == 0:
        LOG.warning("窗口内没有事实数据 —— 所有主题将写入「数据不足」")

    # n 取「真正喂进 prompt 的条数」，不是窗口里的总条数：超预算截断时两者不等，
    # 而 n 要用来校准 validate_insight 的幻觉守卫与层级阈值。
    facts_json, n = fit_json(facts)
    LOG.info("本次喂入事实 %d 条", n)

    system, tpl = load_prompt()
    user = (tpl
            .replace("{{today}}", today())
            .replace("{{window_days}}", str(window_days))
            .replace("{{new_count}}", str(n))
            .replace("{{new_facts_json}}", facts_json)
            .replace("{{recent_facts_json}}", facts_json))

    llm = LLM()
    lcfg = cfg["llm"]
    try:
        items = llm.chat_json(system, user,
                              max_tokens=int(lcfg["max_tokens"]["insight"]),
                              temperature=float(lcfg["temperature"]["insight"]),
                              tag="insight")
    except Exception as e:
        LOG.error("洞察生成失败：%r", e)
        return []

    if isinstance(items, dict):
        items = items.get("insights") or [items]
    if not isinstance(items, list):
        LOG.error("洞察输出不是数组")
        return []

    # 补全缺失的主题为「数据不足」，避免漏主题
    got = {i.get("topic") for i in items if isinstance(i, dict)}
    for t in TOPICS:
        if t not in got:
            items.append({"topic": t, "finding": "数据不足"})

    out, seen_topics = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        cleaned, notes = validate_insight(item, n)
        if not cleaned.get("topic") or cleaned["topic"] in seen_topics:
            continue
        seen_topics.add(cleaned["topic"])
        for nt in notes:
            LOG.info("  [%s] 校验：%s", cleaned["topic"], nt)
        cleaned["date"] = today()
        cleaned["window_days"] = window_days
        if cleaned.get("ai_analysis") is None:
            cleaned["ai_analysis"] = cleaned.get("finding") or ""
        if min_full and cleaned.get("evidence_count", 0) < 3 \
                and cleaned.get("finding") not in ("数据不足", "无增量变化"):
            LOG.info("  [%s] 样本不足 3 条，置信度下调为 low", cleaned["topic"])
            cleaned["confidence"] = "low"
        out.append(cleaned)

    LOG.info("生成 %d 条洞察", len(out))
    return out
