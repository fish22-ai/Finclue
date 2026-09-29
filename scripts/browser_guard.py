# -*- coding: utf-8 -*-
"""把 socai 的 managed Chrome 藏起来，跑完再关掉。

问题
----
socai 抓小红书必须开一个**有界面**的 Chrome：
    C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe
    --user-data-dir=%USERPROFILE%\\.socai\\chrome-profile
它没有 headless 选项（`socai config` 只认 chrome.profile / chrome.profile_dir /
runs.dir / cloud.base_url 四个键），所以每次抓取都会在桌面上弹出一个浏览器窗口。
2026-09-29 用户提出：工作时、尤其视频面试时被弹窗打断是不可接受的。

做法
----
把窗口挪到**虚拟桌面左外侧**，而不是最小化或 SW_HIDE：
  * SW_HIDE / 最小化会让 document.hidden 变真，rAF 和部分懒加载会暂停，
    可能影响 socai 依赖的页面行为；
  * 挪出屏幕后窗口仍处于"可见"状态，Chrome 的渲染与 CDP 输入跟用户正常
    操作时完全一致，只是物理上不在任何显示器的可视区里。
再把它标记成 WS_EX_TOOLWINDOW，让任务栏上也看不见 —— 共享屏幕 / 录屏时
不会露出一个突兀的 Chrome 图标。这一步会 hide+show 一次窗口（几毫秒），
所以包在 try 里，失败也不影响主流程。

安全边界
--------
只碰**命令行里带 .socai\\chrome-profile 的 chrome.exe**。用户日常的 Chrome
不带 --user-data-dir，绝不会被误伤 —— cleanup 尤其重要，绝不能把用户
正在用的浏览器关掉。

用法
----
  # daily.bat 用这个：守窗口 -> 跑 run.py -> 收尾关浏览器（退出码原样透传）
  python scripts\\browser_guard.py --run -- python src\\run.py --stage all --source xhs

  python scripts\\browser_guard.py --cleanup   # 手动收尾（socai stop + 关 Chrome）
  python scripts\\browser_guard.py --probe     # 诊断：列出识别到的进程与窗口位置
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "data", "logs", "cron.log")

# socai 的 managed Chrome 一定带这个 profile 目录；用户日常的 Chrome 不带。
PROFILE_MARKS = (os.path.join(".socai", "chrome-profile"), ".socai/chrome-profile")
SOCAI_EXE = os.path.join(os.path.expanduser("~"), ".socai", "bin", "socai.exe")

# 扫描节奏：没发现浏览器时慢扫（省 CPU），一旦发现就快扫（新窗口/新标签尽快藏好）
POLL_IDLE = 2.0
POLL_ACTIVE = 0.5
# 窗口消失多久算浏览器真关了
GONE_GRACE = 45
# 子进程（run.py）的兜底超时：正常一轮 35 分钟，给到 90 分钟
DEFAULT_CHILD_TIMEOUT = 90 * 60

user32 = ctypes.WinDLL("user32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
WM_CLOSE = 0x0010
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
CREATE_NO_WINDOW = 0x08000000

_log_lock = threading.Lock()


def log(msg: str) -> None:
    """写 cron.log（跟 daily.bat 同一个诊断入口，格式对齐）。

    只写文件、不 print —— daily.bat 里整条命令的 stdout 已经重定向到同一个
    cron.log，再 print 就会每行重复两遍。
    """
    line = "[%s] browser_guard: %s" % (time.strftime("%Y/%m/%d %H:%M:%S"), msg)
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print("%s  (写日志失败：%s)" % (line, exc), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# 进程识别
# --------------------------------------------------------------------------
def socai_chrome_pids() -> list[int]:
    """命令行里带 .socai\\chrome-profile 的 chrome.exe —— 只可能是 socai 的。"""
    if psutil is None:
        return []
    pids = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() != "chrome.exe":
                continue
            cmd = " ".join(proc.info["cmdline"] or [])
            if any(mark in cmd for mark in PROFILE_MARKS):
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return pids


def _windows_of(pids: set[int]) -> list[int]:
    """属于这些 PID 的顶层可见窗口（排除弹出层和无标题的辅助窗口）。"""
    found: list[int] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        if user32.GetWindow(hwnd, GW_OWNER) != 0:  # 有 owner = 弹出层，不是主窗口
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found


def _window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _offscreen_xy() -> tuple[int, int]:
    """整个虚拟桌面左外侧的一个点（多显示器也保证在可视区之外）。"""
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    return vx - vw - 200, vy


def _park(hwnd: int, x: int, y: int) -> bool:
    ok = bool(user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                  SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE))
    if ok:
        # 顺手摘掉任务栏按钮（失败无所谓，屏幕外已经够用了）
        try:
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            if new_ex != ex:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_ex)
                user32.ShowWindow(hwnd, SW_HIDE)
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        except OSError:
            pass
    return ok


def park_all(verbose: bool = True) -> int:
    """把所有 socai Chrome 窗口挪出去，返回处理的窗口数。"""
    pids = socai_chrome_pids()
    if not pids:
        return 0
    x, y = _offscreen_xy()
    n = 0
    for hwnd in _windows_of(set(pids)):
        left, top, w, h = _rect(hwnd)
        if verbose and abs(left - x) <= 8:
            continue  # 已经在屏幕外了，不重复刷日志
        if _park(hwnd, x, y):
            n += 1
            log("已把 socai Chrome 窗口移出屏幕：%r %dx%d -> (%d, %d)"
                % (_window_text(hwnd)[:40], w, h, x, y))
    return n


# --------------------------------------------------------------------------
# 收尾：停 daemon + 关掉 socai 的 Chrome
# --------------------------------------------------------------------------
def _run_quiet(args, timeout=60):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("执行 %s 失败：%s" % (args[0], exc))
        return None


def cleanup(wait: float = 6.0) -> int:
    """停 socai daemon（免得它再拉起浏览器），再关掉 socai 的 Chrome。

    只用 WM_CLOSE 优雅关闭（Chrome 会正常保存 profile，登录态不受影响）；
    超时才降级到 terminate。返回剩余进程数。
    """
    if os.path.exists(SOCAI_EXE):
        _run_quiet([SOCAI_EXE, "stop"])

    pids = socai_chrome_pids()
    if not pids:
        log("没有 socai Chrome 残留")
        return 0

    park_all(verbose=False)  # 关之前先藏好，别让窗口闪一下
    for hwnd in _windows_of(set(pids)):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    log("已请求关闭 socai Chrome（%d 个进程），等待优雅退出…" % len(pids))

    deadline = time.time() + wait
    while time.time() < deadline and socai_chrome_pids():
        time.sleep(0.5)

    left = socai_chrome_pids()
    if left:
        log("还有 %d 个进程没退，强制结束" % len(left))
        for pid in left:
            try:
                psutil.Process(pid).terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(3)
    left = socai_chrome_pids()
    if left:
        log("警告：仍有 %d 个 socai Chrome 进程存活：%s" % (len(left), left))
    else:
        log("socai Chrome 已全部关闭")
    return len(left)


# --------------------------------------------------------------------------
# --run：守窗口 -> 跑子进程 -> 收尾
# --------------------------------------------------------------------------
def _watch_loop(stop: threading.Event) -> None:
    last_seen = None
    seen = False
    while not stop.is_set():
        active = False
        try:
            if socai_chrome_pids():
                seen = True
                active = True
                last_seen = time.time()
                park_all()
            elif seen and last_seen and (time.time() - last_seen) > GONE_GRACE:
                log("socai Chrome 已退出，守护结束")
                return
        except Exception as exc:  # 守护线程绝不能让主流程挂掉
            log("守护异常（已忽略）：%r" % (exc,))
        stop.wait(POLL_ACTIVE if active else POLL_IDLE)


def cmd_run(child: list[str], child_timeout: int) -> int:
    if not child:
        log("--run 后面要跟子命令")
        return 2
    stop = threading.Event()
    t = threading.Thread(target=_watch_loop, args=(stop,), daemon=True)
    t.start()
    log("守护启动：%s" % " ".join(child))

    rc = 1
    try:
        proc = subprocess.Popen(child, cwd=ROOT)
        try:
            rc = proc.wait(timeout=child_timeout)
        except subprocess.TimeoutExpired:
            log("子进程超过 %d 分钟，强制结束" % (child_timeout // 60))
            proc.kill()
            proc.wait(timeout=30)
            rc = 124
    except OSError as exc:
        log("子进程启动失败：%s" % exc)
        rc = 127
    finally:
        stop.set()
        t.join(timeout=5)
        cleanup()

    log("流水线退出码 %d" % rc)
    return rc


def cmd_probe() -> int:
    pids = socai_chrome_pids()
    print("socai Chrome 进程：%s" % (pids or "无"))
    if pids:
        x, y = _offscreen_xy()
        print("目标屏幕外坐标：(%d, %d)" % (x, y))
        for hwnd in _windows_of(set(pids)):
            left, top, w, h = _rect(hwnd)
            print("  窗口 0x%X %r  %dx%d @ (%d, %d)  已藏=%s"
                  % (hwnd, _window_text(hwnd)[:40], w, h, left, top,
                     abs(left - x) <= 8))
    all_chrome = [p.info["pid"] for p in psutil.process_iter(["pid", "name"])
                  if (p.info["name"] or "").lower() == "chrome.exe"]
    print("本机 chrome.exe 总数：%d（其中 socai 的 %d 个）"
          % (len(all_chrome), len(pids)))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    child: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        argv, child = argv[:idx], argv[idx + 1:]

    mode = "run" if child else ("cleanup" if "--cleanup" in argv else
                                "probe" if "--probe" in argv else "run")
    child_timeout = DEFAULT_CHILD_TIMEOUT
    for i, a in enumerate(argv):
        if a == "--child-timeout" and i + 1 < len(argv):
            child_timeout = int(argv[i + 1])

    if mode == "cleanup":
        if psutil is None:
            log("警告：psutil 不可用，无法识别 socai 的 Chrome，跳过清理")
            return 1
        return 1 if cleanup() else 0
    if mode == "probe":
        return cmd_probe()
    if psutil is None:
        log("警告：psutil 不可用，无法隐藏/关闭 socai 的浏览器窗口（流水线照常执行）")
    return cmd_run(child, child_timeout)


if __name__ == "__main__":
    sys.exit(main())
