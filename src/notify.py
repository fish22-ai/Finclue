#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Windows 系统通知（toast）—— 无人值守运行时的「唯一」打扰手段。

设计原则（2026-09-30 与用户对齐）：socai 登录失效需要人工扫码，这是绕不开的
人工环节，但**绝不能以弹出 Chrome 窗口的方式**提醒 —— 用户担心面试/共享屏幕时
突然蹦出小红书页面。改用系统 toast：

  * toast 是右下角一条系统通知，不抢焦点、不弹浏览器、共享屏幕不显眼；
  * 用户看到后在自己方便的时候双击 scripts\\xhs_login.bat 主动扫码；
  * 发送失败静默忽略（通知只是锦上添花，cron.log 里已有完整记录）。

面试保护（2026-09-30 加）：
  用户原话「千万不要面试弹出来」。发通知前先调 SHQueryUserNotificationState
  问 Windows「现在方便打扰吗」——
    全屏应用（D3D fullscreen）/ 演示模式 / 忙碌 / 静默时段 → **先不发**，
    改为挂一个脱离父进程的后台守候（--login-watch），每分钟重查一次，
    等状态恢复正常（面试/会议结束）再发。最多守候 MAX_WAIT_DEFAULT。

实现走 PowerShell 的 Windows.Runtime Toast API（Win10/11 自带，无第三方依赖）。
用 -EncodedCommand（UTF-16LE base64）传脚本，避免中文经 cmd 转码变乱码。
"""

import base64
import ctypes
import os
import subprocess
import sys
import time

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# AUMID 用系统自带 powershell.exe 的路径，免注册也能弹通知
_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# 后台守候：默认最多等 6 小时（面试再长也就这个量级），
# 超时就放弃并记日志 —— 别留一个永远活着的守护进程。
MAX_WAIT_DEFAULT = 6 * 3600
POLL_SECONDS = 60

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "logs", "notify.log")
# 登录失效标记：toast 只是一次性的，用户可能没看到。落一个文件更耐久 ——
# 扫码成功（xhs_login.bat）或下一轮抓取成功（daily.bat）都会删掉它。
LOGIN_FLAG = os.path.join(os.path.dirname(LOG), "login_needed.txt")


def write_login_flag():
    try:
        os.makedirs(os.path.dirname(LOGIN_FLAG), exist_ok=True)
        with open(LOGIN_FLAG, "w", encoding="utf-8") as f:
            f.write("小红书登录已失效（%s）\n请双击 scripts\\xhs_login.bat 扫码。\n"
                    "扫码成功或下一轮抓取成功后本文件会被自动删除。\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S"))
    except OSError:
        pass


def clear_login_flag():
    try:
        os.unlink(LOGIN_FLAG)
        return True
    except OSError:
        return False

# SHQueryUserNotificationState 的取值
QUNS_NOT_PRESENT = 1              # 无用户 / 会话锁屏
QUNS_BUSY = 2                     # 全屏 D3D 之外的应用占用（演示、游戏窗口等）
QUNS_RUNNING_D3D_FULL_SCREEN = 3  # 全屏独占（视频面试、游戏）
QUNS_PRESENTATION_MODE = 4        # 演示模式
QUNS_ACCEPTS_NOTIFICATIONS = 5    # 可以打扰
QUNS_QUIET_TIME = 6               # 静默时段

_DEFER_STATES = {QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN,
                 QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME}


def _winlog(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] notify: %s\n" % (time.strftime("%Y/%m/%d %H:%M:%S"), msg))
    except OSError:
        pass


def notification_state():
    """Windows 认为现在「可不可以打扰」。取不到时保守返回 5（可打扰）。"""
    try:
        v = ctypes.c_int(0)
        rc = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(v))
        return v.value if rc == 0 else QUNS_ACCEPTS_NOTIFICATIONS
    except (OSError, AttributeError):
        return QUNS_ACCEPTS_NOTIFICATIONS


def is_quiet_time():
    """现在是否不该打扰（全屏 / 演示 / 忙碌 / 静默时段）。"""
    return notification_state() in _DEFER_STATES


_PS_TMPL = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$app = '%(aumid)s'
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode('%(title)s')) | Out-Null
$t.Item(1).AppendChild($xml.CreateTextNode('%(msg)s')) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""


def toast(title, msg):
    """发一条系统通知。失败静默（返回 False），绝不影响主流程。"""
    ps = _PS_TMPL % {"aumid": _AUMID, "title": title, "msg": msg}
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


LOGIN_TITLE = "Career Intelligence：小红书登录已失效"
LOGIN_MSG = ("抓取已暂停，未弹任何窗口。方便时双击 "
             "D:\\吃鱿鱼的鱿鱼\\career-intel\\scripts\\xhs_login.bat 扫码，"
             "下一次定时任务会自动恢复。")


def _spawn_login_watcher(max_wait=MAX_WAIT_DEFAULT):
    """后台守候：等系统允许打扰了再发登录提醒。脱离父进程，父进程退出也不影响。"""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--login-watch", "--max-wait", str(int(max_wait))],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
        return True
    except OSError as e:
        _winlog("守候进程启动失败：%r" % (e,))
        return False


def toast_login(wait=True, max_wait=MAX_WAIT_DEFAULT):
    """登录失效提醒。

    现在方便 → 立刻发；正在全屏/演示/忙碌（大概率在面试或开会）→ 挂后台守候，
    等结束了再发。返回 (是否已发出, 是否转入守候)。
    """
    write_login_flag()
    if not wait or not is_quiet_time():
        ok = toast(LOGIN_TITLE, LOGIN_MSG)
        _winlog("登录提醒已发送（state=%d，直接发）" % notification_state())
        return ok, False
    ok = _spawn_login_watcher(max_wait)
    _winlog("当前 state=%d 不宜打扰（疑似全屏面试/演示），已转后台守候 ≤%ds"
            % (notification_state(), max_wait))
    return False, ok


def _login_watch(max_wait):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if not is_quiet_time():
            toast(LOGIN_TITLE, LOGIN_MSG)
            _winlog("守候结束：系统已允许打扰，登录提醒已发送")
            return 0
        time.sleep(POLL_SECONDS)
    _winlog("守候超时（%.1f 小时）仍未等到允许打扰，放弃发送" % (max_wait / 3600.0))
    return 1


def main(argv):
    if "--login-watch" in argv:
        mw = MAX_WAIT_DEFAULT
        if "--max-wait" in argv:
            i = argv.index("--max-wait")
            if i + 1 < len(argv):
                mw = int(argv[i + 1])
        return _login_watch(mw)
    if "--state" in argv:
        print("notification_state=%d quiet=%s"
              % (notification_state(), is_quiet_time()))
        return 0
    title = argv[0] if argv else "测试"
    msg = argv[1] if len(argv) > 1 else "通知内容"
    ok = toast(title, msg)
    print("toast 已发送" if ok else "toast 发送失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
