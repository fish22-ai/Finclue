#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Windows 系统通知（toast）—— 无人值守运行时的「唯一」打扰手段。

设计原则（2026-09-30 与用户对齐）：socai 登录失效需要人工扫码，这是绕不开的
人工环节，但**绝不能以弹出 Chrome 窗口的方式**提醒 —— 用户担心面试/共享屏幕时
突然蹦出小红书页面。改用系统 toast：

  * toast 是右下角一条系统通知，不抢焦点、不弹浏览器、共享屏幕不显眼；
  * 用户看到后在自己方便的时候双击 scripts\\xhs_login.bat 主动扫码；
  * 发送失败静默忽略（通知只是锦上添花，cron.log 里已有完整记录）。

实现走 PowerShell 的 Windows.Runtime Toast API（Win10/11 自带，无第三方依赖）。
用 -EncodedCommand（UTF-16LE base64）传脚本，避免中文经 cmd 转码变乱码。
"""

import base64
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000

# AUMID 用系统自带 powershell.exe 的路径，免注册也能弹通知
_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

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


def toast_login():
    return toast(LOGIN_TITLE, LOGIN_MSG)


if __name__ == "__main__":
    import sys
    ok = toast(sys.argv[1] if len(sys.argv) > 1 else "测试",
               sys.argv[2] if len(sys.argv) > 2 else "通知内容")
    print("toast 已发送" if ok else "toast 发送失败")
