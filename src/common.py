#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""公共工具：配置加载、路径、日志、脱敏、JSON IO。"""

import datetime
import io
import json
import logging
import os
import re
import sys

import yaml

# Windows 控制台默认 GBK，中文日志会变乱码。强制 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")

_cfg_cache = None


def load_config(reload=False):
    global _cfg_cache
    if _cfg_cache is None or reload:
        with io.open(CONFIG_PATH, encoding="utf-8") as f:
            _cfg_cache = yaml.safe_load(f)
    return _cfg_cache


def path(*parts):
    return os.path.join(PROJECT_ROOT, *parts)


def ensure_dir(p):
    if p and not os.path.isdir(p):
        os.makedirs(p)
    return p


def today():
    return datetime.date.today().isoformat()


# --------------------------------------------------------------- 本地按天存档
DATE_FILE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.json$")


def local_dated_files(subdir, prefix=""):
    """data/<subdir>/ 下的 <prefix>YYYY-MM-DD.json → [(date, path)]，按日期升序。

    insight（读事实窗口）与 render（读事实+洞察）共用这一处。
    窗口一律按**文件名日期**判断，不读记录里的 date 字段：本地 date 是
    extract.py 写进去的 ISO 字符串（如 "2026-09-18"），不是飞书那种毫秒时间戳。
    """
    d = path("data", subdir)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if prefix and not fn.startswith(prefix):
            continue
        m = DATE_FILE_RE.search(fn)
        if m:
            out.append((m.group(1), os.path.join(d, fn)))
    out.sort()
    return out


# --------------------------------------------------------------- 脱敏
# 顺序重要：先长后短，避免手机号被更短的规则先吃掉
_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[邮箱已脱敏]"),
    (re.compile(r"(?:微信|微信号|vx|VX|Vx|wechat|WeChat|Wechat)\s*[:：]?\s*[A-Za-z0-9_\-]{5,}"),
     "[联系方式已脱敏]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号已脱敏]"),
    (re.compile(r"(?<!\d)[1-9]\d{4,10}(?!\d)\s*(?:号|QQ|qq)"), "[联系方式已脱敏]"),
    (re.compile(r"(?:身份证|学号)\s*[:：]?\s*[0-9Xx]{6,18}"), "[证件号已脱敏]"),
]


def sanitize(text):
    """正则兜底脱敏。不依赖 LLM，clean 阶段与写入前各跑一次。"""
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def sanitize_deep(obj):
    """递归脱敏 dict / list / str。"""
    if isinstance(obj, str):
        return sanitize(obj)
    if isinstance(obj, dict):
        return {k: sanitize_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_deep(v) for v in obj]
    return obj


# --------------------------------------------------------------- 日志
def make_logger(name="career-intel"):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    log_dir = ensure_dir(path("data", "logs"))
    fh = logging.FileHandler(os.path.join(log_dir, "%s.log" % today()),
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


LOG = make_logger()


# --------------------------------------------------------------- IO
def read_json(p, default=None):
    if not os.path.isfile(p):
        return default
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(p, obj):
    ensure_dir(os.path.dirname(p))
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=1))
    return p


def append_jsonl(p, obj):
    ensure_dir(os.path.dirname(p))
    with io.open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(p):
    if not os.path.isfile(p):
        return []
    out = []
    with io.open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out
