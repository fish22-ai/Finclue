#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书多维表客户端。

照抄了 D:\\xhs-teardown\\scripts\\feishu_write.py 的三个已验证细节：
  1. 单选/多选补选项时必须**全量回传已有选项**，否则老选项被覆盖丢失
  2. 必须读**表 live 字段类型**，不能只信本地 schema
  3. 日期传 epoch 毫秒；超链接传 {"link":..., "text":...} 对象

另有一个 wiki 相关的坑：wiki 内嵌多维表 URL 里的 token 是 wiki_token，
不是 app_token，必须先经 wiki API 解析。
"""

import datetime
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from common import LOG, load_config

# 飞书 Bitable 字段类型
T_TEXT = 1
T_NUMBER = 2
T_SINGLE = 3
T_MULTI = 4
T_DATE = 5
T_CHECKBOX = 7
T_URL = 15
T_ATTACH = 17
T_AUTO_ID = 1005


class FeishuError(RuntimeError):
    pass


TABLES_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "tables.json")


def table_tokens(key, cfg=None):
    """取 (app_token, table_id)。优先 config/tables.json（setup 脚本产物），
    其次 config.yaml 里手填的值。"""
    import io as _io
    if os.path.isfile(TABLES_CACHE):
        with _io.open(TABLES_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
        t = (cache.get("tables") or {}).get(key) or {}
        if cache.get("app_token") and t.get("table_id"):
            return cache["app_token"], t["table_id"]

    cfg = cfg or load_config()
    t = (cfg.get("feishu", {}).get("tables") or {}).get(key) or {}
    if t.get("app_token") and t.get("table_id"):
        return t["app_token"], t["table_id"]
    raise FeishuError(
        "表「%s」还没建或没配置。先跑：python src/setup_feishu.py" % key)


class Feishu(object):
    def __init__(self, cfg=None):
        cfg = cfg or load_config()
        self.cfg = cfg["feishu"]
        # base_url 形如 https://open.feishu.cn/open-apis；
        # 本文件所有 _path 都已带 /open-apis 前缀，故此处剥掉，避免拼成
        # /open-apis/open-apis/... 导致 404
        self.domain = self.cfg["base_url"].rstrip("/")
        if self.domain.endswith("/open-apis"):
            self.domain = self.domain[:-len("/open-apis")]
        self._token = None
        self._token_expire = 0

    # ---------------------------------------------------------- 认证
    def _creds(self):
        f = self.cfg.get("credential_file")
        if not f or not os.path.isfile(f):
            raise FeishuError("找不到飞书凭据文件：%s" % f)
        with io.open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        if not d.get("app_id") or not d.get("app_secret"):
            raise FeishuError("凭据文件缺 app_id / app_secret：%s" % f)
        return d["app_id"], d["app_secret"]

    def token(self):
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        app_id, secret = self._creds()
        r = self._raw("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                      payload={"app_id": app_id, "app_secret": secret})
        if r.get("code") != 0:
            raise FeishuError("获取 tenant_access_token 失败：%s" % r.get("msg"))
        self._token = r["tenant_access_token"]
        self._token_expire = time.time() + int(r.get("expire", 7200))
        return self._token

    # ---------------------------------------------------------- HTTP
    def _raw(self, method, path_, payload=None, token=None, timeout=40,
             retries=5):
        """底层请求。带重试 —— 实测飞书会偶发 SSL UNEXPECTED_EOF
        （本机网络约 1/5 概率失败），每日自动跑的任务不能因一次抖动就整轮失败。
        退避 3s/6s/9s/12s/15s，累计约 45s，足够扛过短时抖动。"""
        url = self.domain + path_
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json; charset=utf-8")
            if token:
                req.add_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except ValueError:
                    last = "HTTP %s: %s" % (e.code, body[:200])
            except Exception as e:
                last = repr(e)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
        return {"code": -1, "msg": "network error after retries: %s" % last}

    def api(self, method, path_, payload=None, timeout=40):
        return self._raw(method, path_, payload, self.token(), timeout)

    # ---------------------------------------------------------- wiki
    def resolve_wiki_node(self, wiki_token):
        """wiki_token -> obj_token(app_token)。结果应缓存，勿频繁调用。"""
        r = self.api("GET", "/open-apis/wiki/v2/spaces/get_node?token=%s"
                     % urllib.parse.quote(wiki_token))
        if r.get("code") != 0:
            raise FeishuError("解析 wiki token 失败：%s（%s）"
                              % (r.get("msg"), wiki_token))
        node = r.get("data", {}).get("node") or {}
        return node.get("obj_token"), node.get("obj_type")

    def parse_wiki_url(self, url):
        """从 wiki URL 里取出 wiki_token 和 table_id。"""
        m = urllib.parse.urlparse(url)
        segs = [s for s in m.path.split("/") if s]
        wiki_token = segs[-1] if segs else None
        q = urllib.parse.parse_qs(m.query)
        table_id = (q.get("table") or [None])[0]
        return wiki_token, table_id

    # ---------------------------------------------------------- 表 / 字段
    def create_app(self, name, folder_token=None):
        payload = {"name": name}
        if folder_token:
            payload["folder_token"] = folder_token
        r = self.api("POST", "/open-apis/bitable/v1/apps", payload)
        if r.get("code") != 0:
            raise FeishuError("建多维表失败：%s" % r.get("msg"))
        app = r["data"]["app"]
        return app.get("app_token"), app.get("url")

    def list_tables(self, app_token):
        r = self.api("GET", "/open-apis/bitable/v1/apps/%s/tables?page_size=100"
                     % app_token)
        if r.get("code") != 0:
            raise FeishuError("读表列表失败：%s" % r.get("msg"))
        return r.get("data", {}).get("items") or []

    def create_table(self, app_token, name, fields=None):
        payload = {"table": {"name": name,
                             "default_view_name": "表格",
                             "fields": fields or []}}
        r = self.api("POST", "/open-apis/bitable/v1/apps/%s/tables"
                     % app_token, payload)
        if r.get("code") != 0:
            raise FeishuError("建数据表失败：%s" % r.get("msg"))
        return r["data"].get("table_id")

    def list_fields(self, app_token, table_id):
        """返回 {字段名: {type, field_id, options:set}}"""
        r = self.api("GET", "/open-apis/bitable/v1/apps/%s/tables/%s/fields"
                     "?page_size=200" % (app_token, table_id))
        if r.get("code") != 0:
            raise FeishuError("读字段失败：%s" % r.get("msg"))
        out = {}
        for i in (r.get("data", {}).get("items") or []):
            opts = set()
            for o in ((i.get("property") or {}).get("options") or []):
                if o.get("name"):
                    opts.add(o["name"])
            out[i.get("field_name")] = {
                "type": i.get("type"),
                "field_id": i.get("field_id"),
                "options": opts,
            }
        return out

    def create_field(self, app_token, table_id, name, ftype, options=None):
        """options 可传 ["A","B"] 或 [{"name":"A"},...]，两种都支持。

        注意：超链接/文本等类型**不能**带空的 property，否则报
        URLFieldPropertyError —— 所以 options 为空时整个 property 键都省掉。
        """
        payload = {"field_name": name, "type": ftype}
        opts = []
        for o in (options or []):
            if isinstance(o, dict):
                opts.append(o if "name" in o else {"name": str(o)})
            else:
                opts.append({"name": str(o)})
        if opts:
            payload["property"] = {"options": opts}
        r = self.api("POST", "/open-apis/bitable/v1/apps/%s/tables/%s/fields"
                     % (app_token, table_id), payload)
        if r.get("code") != 0:
            raise FeishuError("建字段「%s」失败：%s" % (name, r.get("msg")))
        return r["data"].get("field", {}).get("field_id")

    def ensure_options(self, app_token, table_id, field_name, wanted, fields=None):
        """给单选/多选字段补选项。必须全量回传，否则老选项丢失。"""
        fields = fields if fields is not None else self.list_fields(app_token, table_id)
        f = fields.get(field_name)
        if not f:
            LOG.warning("字段「%s」不存在，跳过补选项", field_name)
            return
        missing = [o for o in wanted if o not in f["options"]]
        if not missing:
            return
        merged = list(f["options"]) + missing
        # 保持 wanted 里的顺序在前，避免每次补齐都改变顺序
        merged = [o for o in wanted if o in merged] + \
                 [o for o in merged if o not in wanted]
        r = self.api("PUT",
                     "/open-apis/bitable/v1/apps/%s/tables/%s/fields/%s"
                     % (app_token, table_id, f["field_id"]),
                     {"field_name": field_name, "type": f["type"],
                      "property": {"options": [{"name": n} for n in merged]}})
        if r.get("code") == 0:
            LOG.info("字段「%s」补选项：%s", field_name, "/".join(missing))
            f["options"] = set(merged)
        else:
            LOG.warning("字段「%s」补选项失败：%s", field_name, r.get("msg"))

    # ---------------------------------------------------------- 取值转换
    @staticmethod
    def to_ms(v):
        if isinstance(v, (int, float)):
            return int(v) if v > 1e11 else int(v) * 1000
        s = str(v).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return int(datetime.datetime.strptime(s, fmt).timestamp() * 1000)
            except ValueError:
                continue
        raise ValueError("无法解析日期：%r" % v)

    @staticmethod
    def coerce(name, value, meta, warns):
        t = meta["type"]
        if value is None or value == "" or value == []:
            return None
        if t == T_TEXT:
            return str(value)
        if t == T_NUMBER:
            try:
                f = float(value)
                return int(f) if f.is_integer() else f
            except (TypeError, ValueError):
                warns.append("%s: 「%s」不是数字" % (name, value))
                return None
        if t == T_SINGLE:
            s = str(value)
            if meta["options"] and s not in meta["options"]:
                warns.append("%s: 「%s」不在枚举内" % (name, s))
                return None
            return s
        if t == T_MULTI:
            vals = value if isinstance(value, list) else [value]
            ok = []
            for v in vals:
                s = str(v)
                if meta["options"] and s not in meta["options"]:
                    warns.append("%s: 「%s」不在枚举内" % (name, s))
                else:
                    ok.append(s)
            return ok or None
        if t == T_DATE:
            try:
                return Feishu.to_ms(value)
            except ValueError as e:
                warns.append("%s: %s" % (name, e))
                return None
        if t == T_URL:
            if isinstance(value, dict):
                return value
            return {"link": str(value), "text": str(value)}
        if t == T_CHECKBOX:
            return bool(value)
        warns.append("%s: 不支持的字段类型 %s，已跳过" % (name, t))
        return None

    def build_payload(self, fields_meta, record):
        """按 live 字段类型转换一条记录，返回 (payload, warns, unknown)。"""
        warns, payload, unknown = [], {}, []
        for k, v in (record or {}).items():
            meta = fields_meta.get(k)
            if not meta:
                unknown.append(k)
                continue
            cv = self.coerce(k, v, meta, warns)
            if cv is not None:
                payload[k] = cv
        return payload, warns, unknown

    # ---------------------------------------------------------- 记录
    def create_record(self, app_token, table_id, fields):
        r = self.api("POST", "/open-apis/bitable/v1/apps/%s/tables/%s/records"
                     % (app_token, table_id), {"fields": fields})
        return r

    def batch_create(self, app_token, table_id, records, batch=50):
        """records: list[{字段名: 值}]（已转换）。返回写入成功条数。"""
        ok = 0
        for i in range(0, len(records), batch):
            chunk = records[i:i + batch]
            r = self.api(
                "POST",
                "/open-apis/bitable/v1/apps/%s/tables/%s/records/batch_create"
                % (app_token, table_id),
                {"records": [{"fields": c} for c in chunk]})
            if r.get("code") == 0:
                ok += len(chunk)
            else:
                LOG.warning("批量写入失败（第 %d 批）：%s", i // batch + 1,
                            r.get("msg"))
        return ok

    def find_records(self, app_token, table_id, field_name, value, page_size=1):
        """按字段等值查询，用于幂等去重。查不通返回 None（视为未知）。"""
        r = self.api(
            "POST",
            "/open-apis/bitable/v1/apps/%s/tables/%s/records/search?page_size=%d"
            % (app_token, table_id, page_size),
            {"filter": {"conjunction": "and", "conditions": [
                {"field_name": field_name, "operator": "is",
                 "value": [value]}]}})
        if r.get("code") != 0:
            return None
        return r.get("data", {}).get("items") or []

    def list_records(self, app_token, table_id, page_size=500, max_pages=10):
        """翻页读全部记录（洞察阶段要读近期数据）。"""
        out, page_token = [], None
        for _ in range(max_pages):
            q = "?page_size=%d" % page_size
            if page_token:
                q += "&page_token=" + urllib.parse.quote(page_token)
            r = self.api("GET", "/open-apis/bitable/v1/apps/%s/tables/%s/records%s"
                         % (app_token, table_id, q))
            if r.get("code") != 0:
                LOG.warning("读记录失败：%s", r.get("msg"))
                break
            d = r.get("data", {})
            out.extend(d.get("items") or [])
            if not d.get("has_more"):
                break
            page_token = d.get("page_token")
        return out
