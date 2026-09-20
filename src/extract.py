#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""事实抽取：LLM 逐条抽取 → 严格 JSON → 反幻觉校验。

Prompt 从 prompts/extract_facts.md 读取（`## SYSTEM` 与 `## USER 模板` 两段），
不在代码里另存一份，避免两处不同步。
"""

import hashlib
import io
import json
import os
import re
import time

from common import LOG, load_config, path, read_json, sanitize, write_json
from llm import LLM
from schema import EVIDENCE_REQUIRED, FACTS_KEYS

PROMPT_FILE = path("prompts", "extract_facts.md")


def load_prompt():
    """从 prompts/extract_facts.md 抽 SYSTEM 与 USER 模板。

    ⚠️ SYSTEM 必须从 `## SYSTEM` 一直取到 `## USER 模板` 之前 ——
       中间夹着 `## 字段定义`（含 JSON Schema）等小节。
       早先写成「取到下一个 ## 为止」，把字段定义整段丢了，
       模型因此自己编字段名（输出 position/location 而不是 role/city）。
    """
    with io.open(PROMPT_FILE, encoding="utf-8") as f:
        md = f.read()

    m = re.search(r"^##\s*USER\s*模板\s*$", md, re.M)
    if not m:
        raise RuntimeError("prompts/extract_facts.md 里找不到 `## USER 模板` 段")
    user_start = m.start()

    m = re.search(r"^##\s*SYSTEM\s*$", md, re.M)
    if not m:
        raise RuntimeError("prompts/extract_facts.md 里找不到 `## SYSTEM` 段")
    system = md[m.end():user_start].strip()

    # 去掉 SYSTEM 与 USER 之间的分隔线
    system = re.sub(r"\n-{3,}\s*$", "", system).strip()

    tpl = md[user_start:].split("\n", 1)[1].strip()
    tpl = re.sub(r"^```[a-z]*\s*|\s*```$", "", tpl, flags=re.M).strip()
    return system, tpl


def render_user(tpl, rec):
    hint = {
        "xhs": "小红书笔记。请把 source_type 判为 first_hand（本人经历）、"
               "career_account（求职博主分享）、community（社区讨论）"
               "或 marketing（引流广告）之一",
        "reddit": "Reddit 帖子。请据此选择 source_type 枚举值",
        "linkedin": "LinkedIn 公开招聘岗位。source_type 应为 job_posting",
    }.get(rec.get("source"), "网页内容。请据此选择 source_type 枚举值")
    out = tpl
    out = out.replace("{{source_type_hint}}", hint)
    out = out.replace("{{source_url}}", rec.get("source_url") or "")
    out = out.replace("{{published_at}}", str(rec.get("published_at") or ""))
    out = out.replace("{{content}}", rec.get("content") or "")
    return out


# 小红书的日期只有 "MM-DD"（如 "04-24"），没有年份 —— 补当年
_MD = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def normalize_date(v):
    """把各种日期写法归一成 YYYY-MM-DD。无法识别返回原值。"""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = _MD.match(s)
    if m:
        import datetime
        mm, dd = int(m.group(1)), int(m.group(2))
        year = datetime.date.today().year
        # 若是未来月份（跨年内容），退一年
        try:
            d = datetime.date(year, mm, dd)
        except ValueError:
            return None
        if d > datetime.date.today() + datetime.timedelta(days=7):
            try:
                d = datetime.date(year - 1, mm, dd)
            except ValueError:
                return None
        return d.isoformat()
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return s


# --------------------------------------------------------------- 校验
# 模型有时把 source_type 输出成中文描述而不是枚举码，这里做兜底映射
_SOURCE_TYPE_HINTS = [
    ("job_posting", ["jd", "job posting", "招聘岗位", "招聘公告", "岗位描述",
                     "linkedin", "公开招聘", "招聘页", "hiring"]),
    ("first_hand", ["一手", "本人", "亲历", "个人经历", "first hand", "亲自"]),
    ("career_account", ["求职博主", "职业规划", "博主分享", "career account"]),
    ("community", ["论坛", "社区", "讨论", "转述", "问答", "community"]),
    ("media", ["媒体", "新闻", "报道", "media"]),
    ("marketing", ["营销", "广告", "引流", "推广", "marketing"]),
]


def _map_source_type(v, allowed):
    if not v:
        return None
    s = str(v).strip()
    if s in allowed:
        return s
    low = s.lower()
    for code, hints in _SOURCE_TYPE_HINTS:
        if code not in allowed:
            continue
        if any(h in low for h in hints):
            return code
    return None


# 文本型字段：模型可能返回 list，需要拼成字符串（飞书文本字段装不下 list）
TEXT_FIELDS = [
    "company", "department", "role", "city", "salary", "wlb",
    "recruiting_bar", "actual_bar", "experience_summary",
]

# 已合并进其他字段的旧字段名 —— 模型若残留输出这些，归并后丢弃
MERGED_INTO = {
    "salary_unit": None,
    "working_hours": "wlb",
    "weekend_work": "wlb",
    "education_requirement": "recruiting_bar",
    "internship_requirement": "recruiting_bar",
    "interview_rounds": "experience_summary",
    "interview_content": "experience_summary",
    "return_offer": "experience_summary",
    "position": "role",
    "location": "city",
    "source_account": None,
    "confidence": None,
}


def _join_text(v):
    if isinstance(v, list):
        parts = [str(x).strip() for x in v if str(x).strip()]
        return "；".join(parts) if parts else None
    if isinstance(v, str):
        return v.strip() or None
    return v


def validate(data):
    """反幻觉校验。返回 (cleaned, notes)。不合规的字段置 None，不丢整条。"""
    notes = []
    if not isinstance(data, dict):
        return None, ["模型输出不是对象"]

    out = {k: data.get(k) for k in FACTS_KEYS if k in data}

    # 规则 0a：救回被合并字段的残留输出
    # 模型偶尔会按旧 schema 输出 interview_rounds / weekend_work 等，
    # 直接把内容并进目标字段，别浪费
    for old, target in MERGED_INTO.items():
        v = data.get(old)
        if not v or isinstance(v, (list, dict)) and not v:
            continue
        v = _join_text(v) if isinstance(v, list) else str(v).strip()
        if not v:
            continue
        if target is None:
            continue                      # 已彻底废弃，丢弃
        if target in FACTS_KEYS and not out.get(target):
            out[target] = v
            notes.append("旧字段 %s 的内容并入 %s" % (old, target))

    # 规则 0b：文本字段若被模型返回成 list，拼成字符串
    for k in TEXT_FIELDS:
        if k in out:
            out[k] = _join_text(out[k])

    ev = out.get("evidence")
    if not isinstance(ev, dict):
        ev = {}
    # evidence 的值也可能是 list，同样拼接
    ev = {k: (_join_text(v) if isinstance(v, list) else v)
          for k, v in ev.items()}
    ev = {k: v for k, v in ev.items() if isinstance(v, str) and v.strip()}

    # 规则 1：要求有 evidence 的字段，没有证据就置空
    for k in EVIDENCE_REQUIRED:
        if out.get(k) and not ev.get(k):
            notes.append("%s 有值但无 evidence，已置空" % k)
            out[k] = None

    # 规则 2：recruiting_bar 与 actual_bar 不能引用同一句话
    if out.get("recruiting_bar") and out.get("actual_bar"):
        a, b = ev.get("recruiting_bar", ""), ev.get("actual_bar", "")
        if a and a == b:
            notes.append("recruiting_bar 与 actual_bar 引用同一句证据，actual_bar 置空")
            out["actual_bar"] = None
            ev.pop("actual_bar", None)

    # 规则 3：evidence 里引用的字段必须真的存在且有值
    # 另外剔除元数据键 —— 模型有时会给 source_url / source_type 这类
    # 由模板或代码决定的字段硬造 evidence
    META_KEYS = {"source_url", "source_type", "published_at", "date",
                 "tags", "skip", "skip_reason"}
    for k in list(ev):
        if k not in FACTS_KEYS or k in META_KEYS:
            ev.pop(k)
        elif not out.get(k):
            ev.pop(k)

    # 规则 4：枚举兜底（现在只剩 institution_type 一个枚举字段）
    from schema import INSTITUTION_TYPES, SOURCE_TYPES
    v = out.get("institution_type")
    if v and v not in INSTITUTION_TYPES:
        low = str(v).strip().lower()
        match = next((a for a in INSTITUTION_TYPES if a.lower() == low), None)
        if match:
            out["institution_type"] = match
        else:
            notes.append("institution_type 取值「%s」不在枚举内，已置空" % v)
            out["institution_type"] = None

    # source_type 单独处理：模型爱输出中文描述，走模糊映射
    st = out.get("source_type")
    if st not in SOURCE_TYPES:
        mapped = _map_source_type(st, SOURCE_TYPES)
        if mapped:
            if st:
                notes.append("source_type「%s」→「%s」" % (st, mapped))
            out["source_type"] = mapped
        else:
            if st:
                notes.append("source_type 取值「%s」无法映射，置 unknown" % st)
            out["source_type"] = "unknown"

    # tags 兜底成列表
    tags = out.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,，、]", tags) if t.strip()]
    out["tags"] = tags if isinstance(tags, list) else []

    # 最终再脱敏一次，防模型回填时把脱敏文本还原
    out = {k: (sanitize(v) if isinstance(v, str) else v) for k, v in out.items()}
    out["evidence"] = {k: sanitize(v) for k, v in ev.items()}

    return out, notes


# --------------------------------------------------------------- 抽取缓存
# 逐条落盘，让中断/失败的重跑只补缺失的条，而不是把已经花过钱的整批重抽一遍。
# 缓存里存 prompt 指纹：改了 prompts/extract_facts.md 就自动失效，
# 不会拿旧 schema 的结果冒充新 schema 的结果。
CACHE_SUBDIR = ("data", "extracted")


def prompt_fingerprint(system, user_tpl):
    h = hashlib.sha1()
    h.update(system.encode("utf-8"))
    h.update(b"\x00")
    h.update(user_tpl.encode("utf-8"))
    return h.hexdigest()[:16]


def cache_file(rec):
    key = (rec.get("dedup_key") or rec.get("note_id")
           or rec.get("source_url") or "")
    name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return path(*(CACHE_SUBDIR + (name + ".json",)))


def load_cache(rec, fp):
    d = read_json(cache_file(rec), None)
    if isinstance(d, dict) and d.get("prompt_fingerprint") == fp:
        return d
    return None


def save_cache(rec, fp, entry):
    entry = dict(entry)
    entry["prompt_fingerprint"] = fp
    entry["cached_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    entry["title"] = (rec.get("title") or "")[:60]
    write_json(cache_file(rec), entry)


# --------------------------------------------------------------- 主流程
def to_feishu_record(extracted, rec, date_str):
    """把抽取结果 + 原始记录元信息，映射成飞书字段 dict。"""
    e = extracted
    return {
        "date": date_str,
        "company": e.get("company"),
        "institution_type": e.get("institution_type"),
        "department": e.get("department"),
        "role": e.get("role"),
        "city": e.get("city"),
        "salary": e.get("salary"),
        "wlb": e.get("wlb"),
        "recruiting_bar": e.get("recruiting_bar"),
        "actual_bar": e.get("actual_bar"),
        "experience_summary": e.get("experience_summary"),
        # 方案 A：evidence 存 JSON 字符串，保留逐字段对应关系
        "evidence": json.dumps(e.get("evidence") or {}, ensure_ascii=False),
        "source_type": e.get("source_type") or "unknown",
        "source_url": rec.get("source_url") or "",
        "published_at": normalize_date(rec.get("published_at")),
        "tags": e.get("tags") or [],
    }


def extract_all(records, date_str, limit=None, force=False):
    """逐条抽取。单条失败不影响其他条。返回 (feishu_records, stats)。

    force=True 绕过抽取缓存，无条件重调 LLM（--force 用）。
    """
    system, user_tpl = load_prompt()
    llm = LLM()
    cfg = load_config()["llm"]
    max_tokens = int(cfg["max_tokens"]["extract"])
    temp = float(cfg["temperature"]["extract"])
    fp = prompt_fingerprint(system, user_tpl)

    if limit:
        records = records[:limit]

    out, stats = [], {"skip": 0, "fail": 0, "ok": 0, "cached": 0}
    for i, rec in enumerate(records, 1):
        title = (rec.get("title") or "")[:40]

        # ---- 缓存命中就完全不碰 LLM
        cached = None if force else load_cache(rec, fp)
        if cached is not None:
            if cached.get("skip"):
                LOG.info("[%d/%d] 缓存命中 skip：%s", i, len(records), title)
                stats["cached"] += 1
                stats["skip"] += 1
                continue
            cleaned = cached.get("cleaned")
            if cleaned:
                LOG.info("[%d/%d] 缓存命中：%s", i, len(records), title)
                stats["cached"] += 1
                out.append(to_feishu_record(cleaned, rec, date_str))
                stats["ok"] += 1
                continue

        LOG.info("[%d/%d] 抽取：%s", i, len(records), title)
        try:
            data = llm.chat_json(system, render_user(user_tpl, rec),
                                 max_tokens=max_tokens, temperature=temp,
                                 tag="extract")
        except Exception as e:
            LOG.warning("  失败：%r", e)
            stats["fail"] += 1
            continue

        if not isinstance(data, dict):
            stats["fail"] += 1
            continue
        if data.get("skip"):
            LOG.info("  skip（%s）", data.get("skip_reason"))
            stats["skip"] += 1
            # skip 也缓存：这类「不相关」的判断不会因为重跑而改变
            save_cache(rec, fp, {"skip": True,
                                 "skip_reason": data.get("skip_reason")})
            continue

        cleaned, notes = validate(data)
        if cleaned is None:
            stats["fail"] += 1
            continue
        for n in notes:
            LOG.info("  校验：%s", n)

        save_cache(rec, fp, {"skip": False, "cleaned": cleaned})
        out.append(to_feishu_record(cleaned, rec, date_str))
        stats["ok"] += 1

    LOG.info("抽取完成：成功 %d，skip %d，失败 %d（缓存命中 %d）",
             stats["ok"], stats["skip"], stats["fail"], stats["cached"])
    return out, stats
