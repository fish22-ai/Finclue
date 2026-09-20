#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小红书采集 —— 经本机 socai 二进制（不自研浏览器自动化）。

两阶段：
  1) search --preview  拿卡片（便宜，不开笔记）
  2) get-notes --ocr   取正文 + 逐图 OCR + 评论（贵，慢）

选帖规则：按点赞排序取 Top N 高赞池 → 池中随机抽 M 篇（不直接取 Top M）。

⚠️ socai 会先打印一行 `run_dir: ...` 再输出 JSON —— 必须从第一个 `{` 开始解析。
"""

import datetime
import io
import json
import os
import random
import shutil
import subprocess
import tempfile
import time

from common import (LOG, ensure_dir, load_config, path, read_json,
                    sanitize_deep, today, write_json)
from pipeline import load_seen

DEFAULT_SOCAI = r"C:\Users\吃鱿鱼的鱿鱼\.socai\bin\socai.exe"


def _socai_bin():
    cfg = load_config()
    return (cfg["sources"]["xhs_socai"].get("binary") or DEFAULT_SOCAI)


def run_socai(args, timeout=900):
    """跑 socai，返回 (parsed_json_or_None, run_dir_or_None)。

    socai 输出形如：
        run_dir: C:\\...\\runs\\20260913_100032_xhs_get-notes
        { ...json... }
    所以从第一个 '{' 开始截取。

    ⚠️ 必须用**临时文件**接 stdout/stderr，绝不能用 capture_output=True（管道）。
       socai 是「CLI + 常驻 __daemon」架构：daemon 不存在时由首个 CLI 调用拉起，
       而它会**继承 CLI 的 stdout 管道句柄**。管道因此永远等不到 EOF，
       `subprocess.run(capture_output=True)` 就永久阻塞 —— 连 timeout 都救不回来
       （Windows 上超时后 CPython 会先 kill 再把管道读干「收尸」，同样被卡死）。
       2026-09-18 实测踩过：进程挂死 26 分钟，日志停在调用那一行，
       连 300s 超时日志都没打出来。重定向到文件后 daemon 继承的是文件句柄，
       不影响 EOF 判定，问题根除。
    """
    binp = _socai_bin()
    if not os.path.isfile(binp):
        LOG.error("socai 不存在：%s", binp)
        return None, None

    cmd = [binp] + args
    LOG.info("  $ socai %s", " ".join(args))

    tmpdir = tempfile.mkdtemp(prefix="socai_")
    out_path = os.path.join(tmpdir, "stdout.txt")
    err_path = os.path.join(tmpdir, "stderr.txt")
    try:
        with io.open(out_path, "wb") as fo, io.open(err_path, "wb") as fe:
            p = subprocess.Popen(cmd, stdout=fo, stderr=fe,
                                 stdin=subprocess.DEVNULL)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # 只 kill CLI 自己，不用 /T 杀进程树 —— 免得顺手干掉 daemon
                # （daemon 死了下次要重拉 Chrome，白等一轮）
                try:
                    p.kill()
                except OSError:
                    pass
                LOG.warning("  socai 超时（%ss），已终止", timeout)
                return None, None

        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            out = f.read()
        with io.open(err_path, encoding="utf-8", errors="replace") as f:
            err = f.read()
        rc = p.returncode
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    run_dir = None
    for line in out.splitlines():
        if line.startswith("run_dir:"):
            run_dir = line.split(":", 1)[1].strip()
            break

    i = out.find("{")
    if i < 0:
        LOG.warning("  socai 无 JSON 输出；exit=%s stderr=%s",
                    rc, err[:200])
        return None, run_dir
    try:
        return json.loads(out[i:]), run_dir
    except ValueError as e:
        LOG.warning("  socai JSON 解析失败：%s", e)
        return None, run_dir


def _likes_int(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def _read_artifact(data, run_dir):
    """socai 的 stdout JSON 是**裁剪过**的 —— xsec_token / cover_url 等
    只在 artifact 文件里。必须读 artifact，否则拿不到 xsec_token，
    get-notes 就取不到正文。"""
    path_ = ((data or {}).get("artifact") or {}).get("path")
    cands = []
    if path_:
        cands.append(path_)
    if run_dir:
        import glob as _g
        cands += sorted(_g.glob(os.path.join(run_dir, "artifacts", "*.json")))
    for c in cands:
        c = c.replace("/", os.sep)
        if os.path.isfile(c):
            try:
                with io.open(c, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError) as e:
                LOG.warning("  读 artifact 失败 %s：%r", c, e)
    return None


def search_cards(query, num_notes=30, filters=None, timeout=300):
    """阶段一：搜索卡片。返回 cards 列表（含 xsec_token）。"""
    args = ["xhs", "search", query, "--preview",
            "--num-notes", str(num_notes), "--pretty"]
    for k, v in (filters or {}).items():
        args += ["--filter", "%s=%s" % (k, v)]
    data, run_dir = run_socai(args, timeout=timeout)
    if not data:
        return []

    reason = data.get("reason")
    if reason == "login_required":
        LOG.error("  小红书登录态失效 —— 需要人工重新扫码登录 socai")
        raise RuntimeError("xhs_login_required")
    if reason:
        LOG.warning("  搜索未成功，reason=%s（常见原因：filter 取值非法）", reason)
        return []

    # 优先用 artifact（字段全），stdout 的 cards 只能兜底
    art = _read_artifact(data, run_dir)
    cards = (art or {}).get("cards") or data.get("cards") or []
    if cards and not cards[0].get("xsec_token"):
        LOG.warning("  卡片缺 xsec_token —— artifact 没读到，正文会取不到")
    return cards


def fetch_notes(note_refs, num_comments=8, ocr=True, timeout=900):
    """阶段二：取正文。note_refs: [(note_id, xsec_token), ...]"""
    if not note_refs:
        return []
    args = ["xhs", "get-notes"]
    for nid, tok in note_refs:
        args += ["--note", "%s=%s" % (nid, tok)]
    args += ["--num-comments", str(num_comments)]
    if ocr:
        args += ["--ocr"]
    args += ["--pretty"]

    data, run_dir = run_socai(args, timeout=timeout)
    if not data:
        return []

    # ⚠️ OCR 文本的位置两处不同，必须合并：
    #   stdout  —— 顶层 ocr_text: ["图1文本", "图2文本", ...]（方便，但其余字段是裁剪的）
    #   artifact —— images[i].ocr_text（逐图），顶层没有 ocr_text
    # 只读任意一边都会丢内容：只读 artifact 会丢 stdout 的顶层 ocr_text，
    # 只读 stdout 会丢 xsec_token / 完整评论。
    art = _read_artifact(data, run_dir)
    art_notes = {n.get("entity", n).get("note_id"): (n.get("entity") or n)
                 for n in (art.get("notes") or [])} if art else {}
    out_notes = {}
    for n in (data.get("notes") or []):
        e = n.get("entity") or n
        if e.get("note_id"):
            out_notes[e["note_id"]] = e

    merged = []
    for nid in list(out_notes.keys()) + [k for k in art_notes if k not in out_notes]:
        a = dict(art_notes.get(nid) or {})
        s = dict(out_notes.get(nid) or {})
        # artifact 优先（字段全），stdout 补齐 artifact 缺的键
        m = dict(a)
        for k, v in s.items():
            if k not in m or m[k] in (None, "", [], {}):
                m[k] = v
        m["ocr_text"] = _ocr_from_images(m) or s.get("ocr_text")
        if m.get("note_id"):
            merged.append(m)
    return merged


def _ocr_from_images(entity):
    """把 images[i].ocr_text 拼成一段文本（artifact 的结构）。"""
    parts = []
    for i, im in enumerate(entity.get("images") or [], 1):
        if isinstance(im, dict):
            t = (im.get("ocr_text") or "").strip()
            if t:
                parts.append("【图%d】%s" % (i, t))
    return "\n".join(parts) if parts else None


def _flatten_ocr(ocr_text):
    """ocr_text 可能是 str 或 list[str]（每张图一段）。"""
    if not ocr_text:
        return ""
    if isinstance(ocr_text, str):
        return ocr_text
    if isinstance(ocr_text, list):
        parts = []
        for i, t in enumerate(ocr_text, 1):
            if isinstance(t, str) and t.strip():
                parts.append("【图%d】%s" % (i, t.strip()))
        return "\n".join(parts)
    return str(ocr_text)


def _flatten_comments(comments):
    if not comments:
        return ""
    lines = []
    for c in comments[:20]:
        if isinstance(c, str):
            lines.append(c)
            continue
        t = c.get("text") or ""
        if t:
            lines.append(t)
        for r in (c.get("replies") or [])[:3]:
            if isinstance(r, str) and r:
                lines.append("  ↳ " + r)
    return "\n".join(lines)


# --------------------------------------------------------------- 候选池
# 搜索卡片里的 xsec_token 跨天可复用（2026-09-18 实测：09-13 的 token 在
# 09-18 仍能取到正文），所以把卡片落盘，未抓过的卡够多时整段跳过搜索。
POOL_FILE = ("data", "pool", "xhs.json")


def _pool_path():
    return path(*POOL_FILE)


def _now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def load_pool():
    d = read_json(_pool_path(), {}) or {}
    if not isinstance(d.get("cards"), dict):
        d["cards"] = {}
    return d


def save_pool(pool):
    pool["updated_at"] = pool.get("updated_at") or _now_iso()
    write_json(_pool_path(), pool)
    return _pool_path()


def pool_is_fresh(pool, ttl_days):
    """池子是否在有效期内。没有 updated_at 视为过期。"""
    ts = _parse_iso(pool.get("updated_at"))
    if not ts:
        return False
    return datetime.datetime.now() - ts < datetime.timedelta(days=ttl_days)


def pool_fresh_cards(pool, seen, max_fails=2):
    """未抓过的卡：池里没抓过正文、不在 seen.jsonl 里、且没连续失败太多次。

    三个条件缺一不可 ——
      fetched_at  抓到了正文，但可能被 filter 判为过短/不相关，进不了 seen；
      seen        dedup_key 已落盘，说明这条已经走完「抽取→写入」全流程；
      fail_count  取正文连续失败（多半是 token 已过期），再抽它只是空等一轮。
    只看前两个会重复抓，不管第三个会一直耗在僵尸卡上。
    """
    out = []
    for nid, c in (pool.get("cards") or {}).items():
        if c.get("fetched_at"):
            continue
        if ("xhs:%s" % nid) in seen:
            continue
        if not c.get("xsec_token"):
            continue
        if int(c.get("fail_count") or 0) >= max_fails:
            continue
        c = dict(c)
        c.setdefault("note_id", nid)
        out.append(c)
    return out


def merge_pool(pool, cards, query):
    """把搜索结果并进池子。已有卡片刷新元信息与 token，保留 fetched_at / fail_count。"""
    now = _now_iso()
    into = pool.setdefault("cards", {})
    added = 0
    for c in cards:
        nid = c.get("note_id")
        if not nid:
            continue
        cur = into.get(nid)
        if cur:
            cur["last_seen"] = now
            # 重新搜到时**必须刷新 token** —— 旧的可能已过期，新的一定更新。
            # （早先写成「只在旧的为空时回填」，那会把已失效的 token 一直留着，
            #   每次选中都白跑一次 get-notes。2026-09-18 修。）
            if c.get("xsec_token"):
                cur["xsec_token"] = c["xsec_token"]
            # 卡片元信息可能变化（点赞数会涨），刷新非关键字段
            for k in ("title", "likes", "type", "url", "author"):
                if c.get(k) not in (None, ""):
                    cur[k] = c[k]
        else:
            into[nid] = {
                "note_id": nid,
                "xsec_token": c.get("xsec_token"),
                "title": c.get("title"),
                "likes": c.get("likes"),
                "type": c.get("type"),
                "url": c.get("url"),
                "author": c.get("author"),
                "query": query,
                "first_seen": now,
                "last_seen": now,
                "fetched_at": None,
                "fail_count": 0,
            }
            added += 1
    pool["updated_at"] = now
    return added


def mark_pool_fetched(pool, note_ids):
    """标记这些卡已经成功取到正文，下次不再抽中它们。"""
    now = _now_iso()
    n = 0
    for nid in note_ids:
        c = (pool.get("cards") or {}).get(nid)
        if c is not None and not c.get("fetched_at"):
            c["fetched_at"] = now
            n += 1
    return n


def mark_pool_failed(pool, note_ids):
    """记录取正文失败的卡，连续失败到阈值的会被 pool_fresh_cards 跳过。"""
    now = _now_iso()
    n = 0
    for nid in note_ids:
        c = (pool.get("cards") or {}).get(nid)
        if c is not None:
            c["fail_count"] = int(c.get("fail_count") or 0) + 1
            c["last_fail_at"] = now
            n += 1
    return n


def harvest(source_cfg=None):
    """小红书采集主流程。返回 canonical record 列表。

    在原有「搜索 → 取正文」两阶段之外，多了一层跨天候选池（data/pool/xhs.json）：
    未抓过的卡够多就整段跳过搜索阶段（约 12 分钟）。
    """
    cfg = load_config()
    s = source_cfg or cfg["sources"]["xhs_socai"]
    queries = s.get("queries") or []
    filters = s.get("filters") or {}
    per_query = int(s.get("num_notes_per_query", 30))
    pool_size = int(s.get("high_like_pool", 15))
    pick = int(s.get("posts_to_fetch", 5))
    num_comments = int(s.get("num_comments", 8))
    do_ocr = bool(s.get("ocr", True))
    sleep_s = float(s.get("rate_limit_seconds", 20))
    timeout = int(s.get("timeout_seconds", 900))
    ttl_days = int(s.get("pool_ttl_days", 4))
    min_fresh = int(s.get("pool_min_fresh", 30))
    max_fails = int(s.get("pool_max_fails", 2))

    seen = load_seen()
    pool = load_pool()
    fresh = pool_fresh_cards(pool, seen, max_fails)
    in_ttl = pool_is_fresh(pool, ttl_days)
    LOG.info("候选池：共 %d 张，未抓过 %d 张，%s",
             len(pool.get("cards") or {}), len(fresh),
             "在 %d 天有效期内" % ttl_days if in_ttl else "已过期，需刷新")

    # ---- 阶段一：按需搜索（池子够用就整段跳过）
    if in_ttl and len(fresh) >= min_fresh:
        LOG.info("→ 未抓过 %d 张 ≥ 阈值 %d，跳过搜索阶段", len(fresh), min_fresh)
    else:
        for q in queries:
            LOG.info("小红书 搜索：%s", q)
            try:
                cards = search_cards(q, per_query, filters, timeout=300)
            except RuntimeError as e:
                LOG.error("  终止：%s", e)
                raise
            LOG.info("  卡片 %d 张", len(cards))
            LOG.info("  新增入池 %d 张", merge_pool(pool, cards, q))
            time.sleep(sleep_s)
        save_pool(pool)
        fresh = pool_fresh_cards(pool, seen, max_fails)
        LOG.info("→ 池子现有 %d 张，未抓过 %d 张",
                 len(pool.get("cards") or {}), len(fresh))

    if not fresh:
        LOG.warning("小红书：没有未抓过的卡片（池子空了，等 TTL 过期重搜）")
        return []

    # ---- 选帖：只从「未抓过」的卡里选 → 高赞池 → 池内随机抽
    # 优先图文笔记：视频笔记没有图，--ocr 拿不到东西，正文往往只有一句标题
    images = [c for c in fresh if (c.get("type") or "") == "image"]
    videos = [c for c in fresh if (c.get("type") or "") != "image"]
    if len(images) >= pool_size:
        ranked = images
        LOG.info("未抓过的图文笔记 %d 条 ≥ 池大小 %d，只用图文笔记",
                 len(images), pool_size)
    else:
        ranked = images + videos
        LOG.info("未抓过的图文笔记仅 %d 条，补入 %d 条其他类型",
                 len(images), len(videos))
    ranked.sort(key=lambda c: _likes_int(c.get("likes")), reverse=True)

    top = ranked[:pool_size]
    n = min(pick, len(top))
    chosen = random.sample(top, n)
    LOG.info("未抓过 %d 条 → 高赞池 %d 条 → 随机抽 %d 条",
             len(fresh), len(top), n)

    # ---- 阶段二：逐条取正文（逐条调用，便于单条失败不影响其他）
    records, got, failed = [], [], []
    for c in chosen:
        nid, tok = c.get("note_id"), c.get("xsec_token")
        if not tok:
            LOG.warning("  跳过 %s：无 xsec_token", nid)
            continue
        LOG.info("取正文：%s（%s 赞）", (c.get("title") or "")[:30], c.get("likes"))
        try:
            notes = fetch_notes([(nid, tok)], num_comments, do_ocr, timeout)
        except Exception as e:
            LOG.warning("  失败：%r", e)
            notes = []
        if not notes:
            LOG.warning("  没取到正文（token 可能已过期），下轮重试")
            failed.append(nid)
            continue
        got.append(nid)

        for e in notes:
            body = e.get("content") or ""
            ocr = _flatten_ocr(e.get("ocr_text"))
            comments = _flatten_comments(e.get("top_comments"))
            records.append({
                "source": "xhs",
                "source_url": e.get("url") or c.get("url") or "",
                "note_id": nid,
                "title": e.get("title") or c.get("title") or "",
                "author": e.get("author") or c.get("author") or "",
                "published_at": e.get("date") or "",
                "likes": _likes_int(c.get("likes")),
                "body": body,
                "ocr_text": ocr,
                "comments": comments,
                "hashtags": e.get("hashtags") or [],
                "location": e.get("location") or "",
                "query": None,
            })
        time.sleep(sleep_s)

    # 真取到正文的标记「已抓」；失败的记 fail_count，连续失败到阈值的下轮不再抽中
    if got:
        LOG.info("标记已抓 %d 张（池内共 %d 张）",
                 mark_pool_fetched(pool, got), len(pool.get("cards") or {}))
    if failed:
        if got:
            LOG.info("标记失败 %d 张（连续 %d 次后不再抽中）",
                     mark_pool_failed(pool, failed), max_fails)
        else:
            # 一轮里**全军覆没**多半是会话级问题（登录态失效 / daemon 挂了），
            # 不是这些卡本身有问题。此时记 fail_count 会在下一轮把整批好卡拉黑，
            # 池子静默萎缩 —— 所以只在「至少有一张成功」时才归因到单卡。
            LOG.warning("本轮 %d 张全部失败，判为会话级问题（登录态/daemon），"
                        "不记 fail_count，避免误伤好卡", len(failed))
    save_pool(pool)

    return sanitize_deep(records)
