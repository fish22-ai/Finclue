#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书两张表的字段定义 —— 单一真源。

建表脚本与抽取校验都从这里读，不要在别处硬编码字段名。
枚举值严格取自项目需求，不得自行扩充。
"""

from feishu import T_DATE, T_MULTI, T_NUMBER, T_SINGLE, T_TEXT, T_URL

INSTITUTION_TYPES = [
    "券商", "投行", "行研", "PE", "VC", "公募", "私募", "资管",
    "银行", "信托", "保险", "量化", "金科", "财资", "风控", "财会", "其他",
]

SALARY_UNITS = [
    "monthly_rmb", "daily_rmb", "annual_rmb",
    "monthly_hkd", "annual_hkd",
    "monthly_usd", "annual_usd", "other", "unknown",
]

WLB = ["good", "medium", "bad", "unknown"]
WEEKEND_WORK = ["yes", "no", "occasional", "unknown"]
RETURN_OFFER = ["yes", "no", "unclear", "na"]

SOURCE_TYPES = [
    "first_hand", "career_account", "community",
    "job_posting", "media", "marketing", "unknown",
]

CONFIDENCE = ["high", "medium", "low"]

TAG_OPTIONS = [
    "暑期实习", "秋招", "春招", "日常实习", "留用", "转正",
    "一面", "二面", "终面", "笔试", "群面",
    "外资", "中资", "港资", "国企", "互联网",
    "高薪", "加班多", "WLB好", "出差多",
    "量化", "投研", "交易", "销售", "中后台",
]

TOPICS = ["recruiting_bar", "wlb", "salary", "hc_trend", "interview", "return_offer"]
FINDING_LAYERS = ["fact", "pattern", "interpretation", "implication"]


# ------------------------------------------------------------ 表 1 事实表
# 2026-09-15 精简：用户要求「每天就扫一眼」，从 26 字段砍到 13 + 3 隐藏。
#   wlb 改为文本 —— 单选装不下「早9晚9、周末偶尔加班」这类细节，
#   合并了原 working_hours / weekend_work。
#   recruiting_bar 合并了原 education_requirement / internship_requirement。
#   experience_summary 合并了原 interview_rounds / interview_content / return_offer。
FACTS_TABLE_NAME = "Career Intelligence"

FACTS_FIELDS = [
    ("date", T_DATE, None, "抓取日期（非发布时间），增量窗口靠它切片"),
    ("company", T_TEXT, None, "机构名，保留原文写法"),
    ("institution_type", T_SINGLE, INSTITUTION_TYPES, ""),
    ("department", T_TEXT, None, "部门/组"),
    ("role", T_TEXT, None, "岗位名"),
    ("city", T_TEXT, None, "用文本：城市写法太杂，单选会炸出上百选项"),
    ("salary", T_TEXT, None, "原文片段，不换算"),
    ("wlb", T_TEXT, None, "一句人话。含工作时长、周末是否加班、强度感受"),
    ("recruiting_bar", T_TEXT, None, "JD 书面门槛。含学历/专业/实习经历/证书要求"),
    ("actual_bar", T_TEXT, None, "真实录取门槛 —— 与 recruiting_bar 严格分开"),
    ("experience_summary", T_TEXT, None,
     "经历摘要。含面试轮次与内容、留用/转正情况"),
    ("source_url", T_URL, None, "业务幂等键"),
    ("tags", T_MULTI, TAG_OPTIONS, ""),

    # --- 以下 3 个字段保留但对日常浏览价值低，建议在飞书视图里隐藏 ---
    # published_at：时效性判断（面经过期很快），但日期已在 date 列可见
    # source_type ：一手经历 / 求职博主 / 引流广告 —— 决定这条值不值得信
    # evidence    ：AI 禁脑补的审计依据，砍了就再也追溯不了每个字段的原文出处
    ("published_at", T_DATE, None, "内容发布时间。[建议视图隐藏]"),
    ("source_type", T_SINGLE, SOURCE_TYPES, "可信度信号。[建议视图隐藏]"),
    ("evidence", T_TEXT, None, "JSON 字符串：{字段名: 原文引用}。[建议视图隐藏]"),
]

# 日常浏览需要看到的列（其余建议在飞书视图里隐藏）
FACTS_VISIBLE = [
    "date", "company", "institution_type", "department", "role", "city",
    "salary", "wlb", "recruiting_bar", "actual_bar", "experience_summary",
    "source_url", "tags",
]

# 抽取结果里允许出现的键（用于过滤模型多输出的字段）
FACTS_KEYS = [f[0] for f in FACTS_FIELDS] + ["skip", "skip_reason"]

# 需要校验「有值必须有 evidence」的字段
EVIDENCE_REQUIRED = [
    "salary", "wlb", "recruiting_bar", "actual_bar", "experience_summary",
]


# ------------------------------------------------------------ 表 2 洞察表
INSIGHTS_TABLE_NAME = "Career Insights"

INSIGHTS_FIELDS = [
    ("date", T_DATE, None, "分析生成日"),
    ("topic", T_SINGLE, TOPICS, ""),
    ("finding", T_TEXT, None, "数据不足 / 无增量变化 时为对应文案"),
    ("finding_layer", T_SINGLE, FINDING_LAYERS, "落实 Fact/Pattern/Interpretation/Implication 四层分离"),
    ("why_it_matters", T_TEXT, None, ""),
    ("industry_implication", T_TEXT, None, ""),
    ("career_implication", T_TEXT, None, ""),
    ("evidence_count", T_NUMBER, None, "支撑条数，用于反幻觉校验"),
    ("related_cases", T_TEXT, None, "多值用逗号分隔"),
    ("confidence", T_SINGLE, CONFIDENCE, ""),
    ("ai_analysis", T_TEXT, None, "完整分析正文"),
    ("window_days", T_NUMBER, None, "该条分析覆盖的窗口天数"),
]

INSIGHTS_KEYS = [f[0] for f in INSIGHTS_FIELDS]

# topic 为单选字段，但洞察结果一条记录一个主题；冗余保留便于扩展


def to_feishu_field_spec(fields):
    """转成飞书建表 API 需要的 field 结构。"""
    out = []
    for name, ftype, options, _desc in fields:
        spec = {"field_name": name, "type": ftype}
        if options:
            spec["property"] = {"options": [{"name": o} for o in options]}
        out.append(spec)
    return out
