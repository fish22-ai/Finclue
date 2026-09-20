#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""建表脚本：创建 Career Intelligence 两张飞书多维表并写入全部字段。

用法：
    python setup_feishu.py --dry-run     # 只看会建什么，不实际创建
    python setup_feishu.py               # 实际创建

产物：config/tables.json（含 app_token / table_id / url）
      —— 不写进 config.yaml，避免 yaml 重排丢注释
"""

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import LOG, ensure_dir, path, write_json  # noqa: E402
from feishu import Feishu, FeishuError  # noqa: E402
from schema import (FACTS_FIELDS, FACTS_TABLE_NAME, INSIGHTS_FIELDS,  # noqa: E402
                    INSIGHTS_TABLE_NAME, to_feishu_field_spec)

TABLES_CACHE = path("config", "tables.json")
APP_NAME = "Career Intelligence"


def find_existing_table(fs, app_token, name):
    for t in fs.list_tables(app_token):
        if t.get("name") == name:
            return t.get("table_id")
    return None


def sync_fields(fs, app_token, table_id, tfields, tname, prune=True):
    """按 schema 对齐表的字段：补缺失、删多余。

    schema.py 是唯一真源 —— 改了 FACTS_FIELDS / INSIGHTS_FIELDS 再跑本脚本即可。
    prune 会**删除**表里不在 schema 中的字段（连带该列数据），
    这是把 26 字段精简到 16 字段用的。
    """
    want = {f[0]: f for f in tfields}
    live = fs.list_fields(app_token, table_id)

    created = skipped = failed = pruned = 0
    for name, ftype, options, _desc in tfields:
        if name in live:
            skipped += 1
            continue
        try:
            fs.create_field(app_token, table_id, name, ftype, options)
            created += 1
            LOG.info("  + 字段「%s」", name)
        except FeishuError as e:
            failed += 1
            LOG.warning("  ! 字段「%s」失败：%s", name, e)

    if prune:
        for name, meta in list(live.items()):
            # 飞书建表自带的首列（如「文本」）不在 schema 里，也一并清掉
            if name in want:
                continue
            r = fs.api("DELETE",
                       "/open-apis/bitable/v1/apps/%s/tables/%s/fields/%s"
                       % (app_token, table_id, meta["field_id"]))
            if r.get("code") == 0:
                pruned += 1
                LOG.info("  - 删除多余字段「%s」", name)
            else:
                LOG.warning("  ! 删除字段「%s」失败：%s", name, r.get("msg"))

    LOG.info("表「%s」：新建 %d，已存在 %d，删除 %d，失败 %d",
             tname, created, skipped, pruned, failed)
    return created, pruned, failed


def setup(dry_run=False):
    fs = Feishu()

    LOG.info("飞书认证……")
    try:
        fs.token()
    except FeishuError as e:
        LOG.error("认证失败：%s", e)
        return 1
    LOG.info("认证成功")

    existing = {}
    if os.path.isfile(TABLES_CACHE):
        with io.open(TABLES_CACHE, encoding="utf-8") as f:
            existing = json.load(f)
    app_token = (existing.get("app_token") or "").strip() or None

    if dry_run:
        LOG.info("[dry] 将创建 app「%s」", APP_NAME)
        LOG.info("[dry] 表「%s」%d 个字段", FACTS_TABLE_NAME, len(FACTS_FIELDS))
        LOG.info("[dry] 表「%s」%d 个字段", INSIGHTS_TABLE_NAME, len(INSIGHTS_FIELDS))
        return 0

    # ---- 1. 建 app（或复用已记录的）
    if app_token:
        LOG.info("复用已有 app_token：%s", app_token)
    else:
        LOG.info("创建多维表 app「%s」……", APP_NAME)
        app_token, url = fs.create_app(APP_NAME)
        LOG.info("app 创建成功：%s", app_token)
        LOG.info("  请在飞书中打开并确认：%s", url)

    # ---- 2. 建两张表 + 字段
    result = {"app_token": app_token, "tables": {}}

    for tname, tfields, tkey in (
            (FACTS_TABLE_NAME, FACTS_FIELDS, "facts"),
            (INSIGHTS_TABLE_NAME, INSIGHTS_FIELDS, "insights")):

        table_id = find_existing_table(fs, app_token, tname)
        if table_id:
            LOG.info("表「%s」已存在：%s", tname, table_id)
        else:
            LOG.info("创建表「%s」……", tname)
            # 飞书建表时至少要有字段；先只传第一个，其余由 sync_fields 补齐
            specs = to_feishu_field_spec(tfields)
            table_id = fs.create_table(app_token, tname, specs[:1])
            LOG.info("  表创建成功：%s", table_id)

        sync_fields(fs, app_token, table_id, tfields, tname, prune=True)
        result["tables"][tkey] = {
            "name": tname, "table_id": table_id,
            "url": "https://feishu.cn/base/%s?table=%s" % (app_token, table_id),
        }

    # ---- 3. 落盘
    ensure_dir(path("config"))
    write_json(TABLES_CACHE, result)
    LOG.info("已写入 %s", TABLES_CACHE)

    print("")
    print("=" * 62)
    print("建表完成。app_token = %s" % app_token)
    for k, v in result["tables"].items():
        print("  %-9s %s  table_id=%s" % (k, v["name"], v["table_id"]))
    print("=" * 62)
    print("")
    print("⚠️  还需要一步：把这个应用加为表格协作者，否则后续写入会 403。")
    print("    应用 app_id = cli_aa01bbf3d5f8dbd3")
    print("    在表格右上角「分享」里添加该应用为可编辑协作者。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    return setup(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
