#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把渲染产物推上 GitHub Pages —— 手机 PWA 靠这一步才能看到新一期。

背景（2026-09-29 诊断）：流水线此前从不 push，新一期只落在本地 docs/，
GitHub Pages 一直停在 2026-09-20，所以手机上永远等不到更新。

只在 docs/ 有变化时才提交，而且**只**提交 docs/：
  - data/、config/ 在 .gitignore 里（公开仓库的隐私边界）。这里不用 `git add -A`
    全量暂存，正是为了避免哪天 .gitignore 被改坏后，把抓取到的他人正文顺手推上去。
  - src/、scripts/ 等代码改动仍由人工提交 —— 自动流程不该替你决定代码何时上线。

退出码：0 = 已推送或无变化 / 1 = git 不可用 / 2 = 推送失败（原因在 cron.log）
手动补推：python scripts\\push_site.py
"""

import datetime
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(msg):
    print("[push_site] %s" % msg, flush=True)


def git(*args):
    """跑一条 git，返回 (returncode, stdout+stderr)。"""
    env = dict(os.environ)
    # 没有交互终端，别让 git 卡在凭据提示上等 2 小时（daily.bat 里也是这个值）。
    env["GIT_TERMINAL_PROMPT"] = "0"
    # 输出按 UTF-8 收；分支名/路径里有中文也不会炸。
    p = subprocess.run(["git"] + list(args), cwd=ROOT, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def main():
    rc, out = git("--version")
    if rc != 0:
        log("找不到可用的 git，跳过推送。%s" % out)
        return 1

    rc, out = git("status", "--porcelain", "--", "docs")
    if rc != 0:
        log("git status 失败：%s" % out)
        return 2
    if not out:
        log("docs/ 无变化，无需推送。")
        return 0

    log("docs/ 有变化：\n%s" % out)

    rc, out = git("add", "-A", "--", "docs")
    if rc != 0:
        log("git add 失败：%s" % out)
        return 2

    rc, _ = git("diff", "--cached", "--quiet")
    if rc == 0:
        log("暂存区无实际改动（可能只有换行/权限差异），跳过提交。")
        return 0

    today = datetime.date.today().isoformat()
    rc, out = git("commit", "-m", "site: 自动更新日报 %s" % today)
    if rc != 0:
        log("git commit 失败：%s" % out)
        return 2
    first_line = out.splitlines()[0] if out else "已提交"
    log("已提交：%s" % first_line)

    rc, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or not branch:
        branch = "main"
    rc, out = git("push", "origin", branch)
    if rc != 0:
        log("git push 失败（手机上不会看到这一期）：%s" % out)
        log("网络恢复后手动补推：python scripts\\push_site.py")
        return 2

    log("已推送到 origin/%s，GitHub Pages 稍后自动生效（约 1 分钟）。" % branch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
