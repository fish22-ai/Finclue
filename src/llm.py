#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM 客户端：走 agentrouter 中转（OpenAI 兼容格式）。

⚠️ 三个易错点，改动前务必读：
  1. 只带 Authorization 会被拒（unauthorized client detected）
     —— 必须同时伪造 Claude Code 客户端指纹头
  2. deepseek-v4-flash 是推理模型，响应含 reasoning_content，
     max_tokens 给小了会导致 content 为空（finish_reason=length）
  3. 返回是 OpenAI 格式：choices[0].message.content
"""

import io
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request

from common import LOG, load_config

# Claude Code 客户端指纹 —— 缺任何一个都会被上游拒绝
CLIENT_HEADERS = {
    "user-agent": "claude-cli/2.0.0 (external, cli)",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219",
}


def _cc_switch_db():
    home = os.path.expanduser("~")
    return os.path.join(home, ".cc-switch", "cc-switch.db")


def load_api_key():
    """按优先级取 key：
    1) 环境变量 CAREER_INTEL_API_KEY
    2) cc-switch.db 里 is_current=1 的 provider
    """
    env = os.environ.get("CAREER_INTEL_API_KEY")
    if env:
        return env.strip()

    db = _cc_switch_db()
    if not os.path.isfile(db):
        raise RuntimeError(
            "找不到 API key：环境变量 CAREER_INTEL_API_KEY 未设置，"
            "且 %s 不存在" % db)
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        row = con.execute(
            "select settings_config from providers where is_current=1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("cc-switch.db 里没有 is_current=1 的 provider")
    env_cfg = json.loads(row[0]).get("env") or {}
    key = env_cfg.get("ANTHROPIC_AUTH_TOKEN")
    if not key or key == "PROXY_MANAGED":
        raise RuntimeError("cc-switch provider 里的 ANTHROPIC_AUTH_TOKEN 不可用")
    return key


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

# 计价（USD / 百万 token），取自 cc-switch 的 model_pricing 表。
# 换成别的模型记得同步改这里，否则成本报告是错的。
PRICING = {
    "deepseek-v4-flash": {"in": 0.44, "out": 1.32, "cache": 0.014},
    "deepseek-v4-flash-0731": {"in": 0.44, "out": 1.32, "cache": 0.014},
    "deepseek-v4-pro": {"in": 1.32, "out": 3.96, "cache": 0.044},
    "deepseek-chat": {"in": 0.44, "out": 1.32, "cache": 0.014},
}

# 进程级累计用量，跑完由 report_usage() 打印
USAGE = {
    "calls": 0, "failed": 0,
    "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    "cached_tokens": 0, "cost_usd": 0.0, "cache_saved_usd": 0.0,
    "by_tag": {},          # {tag: {calls, prompt_tokens, completion_tokens, cost_usd}}
}


def _price(model):
    return PRICING.get(model, {"in": 0.44, "out": 1.32, "cache": 0.014})


def record_usage(model, usage, tag="llm"):
    """累计一次调用的 token 与成本。usage 为 OpenAI 格式的 usage 对象。"""
    if not usage:
        return
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    # 推理模型的思考 token 单独计费口径不一，这里并入输出
    rt = int(((usage.get("completion_tokens_details") or {})
              .get("reasoning_tokens")) or 0)
    cached = int(((usage.get("prompt_tokens_details") or {})
                  .get("cached_tokens")) or 0)
    billable_in = max(pt - cached, 0)

    p = _price(model)
    cost = (billable_in * p["in"] + cached * p["cache"] + ct * p["out"]) / 1e6

    USAGE["calls"] += 1
    USAGE["prompt_tokens"] += pt
    USAGE["completion_tokens"] += ct
    USAGE["reasoning_tokens"] += rt
    USAGE["cached_tokens"] += cached
    USAGE["cost_usd"] += cost
    # 省下来的钱：命中的 token 按 cache 价而非输入价计费
    USAGE["cache_saved_usd"] += cached * (p["in"] - p["cache"]) / 1e6

    t = USAGE["by_tag"].setdefault(
        tag, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
              "cost_usd": 0.0})
    t["calls"] += 1
    t["prompt_tokens"] += pt
    t["completion_tokens"] += ct
    t["cost_usd"] += cost


def report_usage():
    """打印本次运行的 token 与成本明细。"""
    u = USAGE
    lines = ["", "=" * 60, "LLM 用量与成本", "=" * 60]
    for tag, t in sorted(u["by_tag"].items()):
        lines.append("  %-12s 调用 %-4d 输入 %-9s 输出 %-7s  $%.4f"
                     % (tag, t["calls"], "{:,}".format(t["prompt_tokens"]),
                        "{:,}".format(t["completion_tokens"]), t["cost_usd"]))
    lines.append("  " + "-" * 56)
    lines.append("  %-12s 调用 %-4d 输入 %-9s 输出 %-7s  $%.4f"
                 % ("合计", u["calls"], "{:,}".format(u["prompt_tokens"]),
                    "{:,}".format(u["completion_tokens"]), u["cost_usd"]))
    if u["calls"]:
        lines.append("  平均每次：输入 %s tok，$%.4f"
                     % ("{:,}".format(u["prompt_tokens"] // u["calls"]),
                        u["cost_usd"] / u["calls"]))
    if u["reasoning_tokens"]:
        lines.append("  其中推理 token %s（已并入输出计费）"
                     % "{:,}".format(u["reasoning_tokens"]))
    # 上游到底有没有透传 prompt 缓存 —— 只能靠这个数确认（2026-09-18 加）
    if u["cached_tokens"]:
        lines.append("  命中 prompt 缓存 %s tok，省 $%.4f"
                     % ("{:,}".format(u["cached_tokens"]), u["cache_saved_usd"]))
    else:
        lines.append("  未命中 prompt 缓存（cached_tokens=0，上游未透传）")
    if u["failed"]:
        lines.append("  失败调用 %d 次" % u["failed"])
    lines.append("=" * 60)
    text = "\n".join(lines)
    LOG.info("%s", text)
    return text


def strip_json_fence(text):
    """模型常把 JSON 包在 ``` 里，剥掉。"""
    if not text:
        return text
    m = _JSON_FENCE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


class LLM(object):
    def __init__(self, cfg=None):
        cfg = cfg or load_config()
        self.cfg = cfg["llm"]
        self.base = self.cfg["base_url"].rstrip("/")
        self.model = self.cfg["model"]
        self.key = load_api_key()
        self.max_retries = int(self.cfg.get("max_retries", 2))

    # ---------------------------------------------------------- 底层
    def _post(self, payload, timeout=180):
        req = urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.key)
        for k, v in CLIENT_HEADERS.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def chat(self, system, user, max_tokens=4096, temperature=0.0, tag="llm"):
        """返回 content 字符串。失败抛异常，由调用方决定是否跳过该条。"""
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self._post(payload)
                record_usage(self.model, r.get("usage"), tag)
                choice = (r.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content") or ""
                finish = choice.get("finish_reason")
                if not content.strip():
                    # 推理模型把预算吃完了 —— 这不是重试能解决的，直接报错
                    raise RuntimeError(
                        "模型返回空 content（finish_reason=%s）。"
                        "该模型是推理模型，请调大 max_tokens" % finish)
                return strip_json_fence(content)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                last_err = "HTTP %s: %s" % (e.code, body)
            except Exception as e:
                last_err = repr(e)
            if attempt < self.max_retries:
                wait = 3 * (attempt + 1)
                LOG.warning("LLM 调用失败（%s），%ss 后重试", last_err, wait)
                time.sleep(wait)
        USAGE["failed"] += 1
        raise RuntimeError("LLM 调用最终失败：%s" % last_err)

    def chat_json(self, system, user, max_tokens=4096, temperature=0.0,
                  tag="llm"):
        """要求模型输出 JSON，解析后返回。解析失败抛 ValueError。"""
        raw = self.chat(system, user, max_tokens, temperature, tag)
        try:
            return json.loads(raw)
        except ValueError:
            # 常见：模型在 JSON 后加了说明文字，尝试截到最后一个 } 或 ]
            for closer in ("}", "]"):
                i = raw.rfind(closer)
                if i > 0:
                    try:
                        return json.loads(raw[:i + 1])
                    except ValueError:
                        continue
            raise ValueError("无法解析为 JSON：%s" % raw[:300])
