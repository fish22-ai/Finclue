#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""写入飞书：按 live 字段类型转换 → 幂等去重 → 批量写入。

先写本地 canonical JSON，再推飞书；推送失败不丢数据。
（这条范式来自 D:\\xhs-teardown 的教训）
"""

import os

from common import LOG, ensure_dir, load_config, path, sanitize_deep, write_json
from feishu import Feishu, table_tokens


def save_local(records, date_str, name):
    """先把待写记录落盘，保证推送失败不丢数据。"""
    p = path("data", "pending", "%s_%s.json" % (name, date_str))
    write_json(p, records)
    LOG.info("本地暂存 %d 条 → %s", len(records), p)
    return p


def write_records(records, table_key, date_str, dry_run=False):
    """写入指定表。返回写入成功条数。"""
    if not records:
        LOG.info("没有记录要写入 %s", table_key)
        return 0

    # 开关必须在 fs = Feishu() **之前** —— 放低了照样会走网络。
    # 关掉之后飞书只是可选镜像：阅读端在 site/，洞察窗口读 data/facts/（见 insight.read_facts），
    # 记录本身仍由调用方的 save_local 落到 data/pending/，所以内容不丢。
    # dry_run 是例外：它现在是唯一能拿 live 字段类型校验字段映射的自检手段，
    # 关掉写入后更需要它，不能一起掐掉。
    if not dry_run and not load_config().get("feishu", {}) \
            .get("write", {}).get("enabled", True):
        LOG.info("[关闭] feishu.write.enabled=false，跳过写入 %s（%d 条已存本地 pending）",
                 table_key, len(records))
        return 0

    fs = Feishu()
    app_token, table_id = table_tokens(table_key)
    fields_meta = fs.list_fields(app_token, table_id)
    LOG.info("目标表 %s：%d 个字段", table_key, len(fields_meta))

    # tags 是自由生成的，不可能预设全 —— 写入前把本批出现的新标签补进选项，
    # 否则 coerce 会把它们全部丢弃（飞书多选字段只接受已存在的选项）
    if not dry_run and "tags" in fields_meta:
        wanted = []
        for rec in records:
            for t in (rec.get("tags") or []):
                t = str(t).strip()
                if t and t not in wanted:
                    wanted.append(t)
        if wanted:
            fs.ensure_options(app_token, table_id, "tags", wanted, fields_meta)

    payloads, skipped = [], 0
    for rec in records:
        rec = sanitize_deep(rec)
        payload, warns, unknown = fs.build_payload(fields_meta, rec)
        for w in warns:
            LOG.warning("  字段告警：%s", w)
        if unknown:
            LOG.warning("  表里没有这些字段，已跳过：%s", "、".join(unknown))
        if not payload:
            skipped += 1
            continue
        payloads.append(payload)

    if dry_run:
        LOG.info("[dry] 将写入 %d 条（跳过 %d 条）", len(payloads), skipped)
        if payloads:
            LOG.info("[dry] 首条样例：%s", list(payloads[0].keys()))
        return 0

    n = fs.batch_create(app_token, table_id, payloads)
    LOG.info("写入 %s：成功 %d 条", table_key, n)

    # 写入成功的落一份 archive
    ensure_dir(path("data", "written"))
    write_json(path("data", "written", "%s_%s.json" % (table_key, date_str)),
               payloads)
    return n


def exists_in_table(rec_value, table_key, field="source_url"):
    """按字段查飞书表是否存在（次级幂等保险）。

    表没有唯一约束，所以本地 seen.jsonl 才是主去重；这里只是兜底。
    查不通返回 None。
    """
    try:
        fs = Feishu()
        app_token, table_id = table_tokens(table_key)
        return fs.find_records(app_token, table_id, field, rec_value)
    except Exception as e:
        LOG.warning("查重失败（忽略）：%r", e)
        return None
