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
1) 把窗口挪到**虚拟桌面右外侧**（2026-09-29 先放左侧，2026-09-30 用户反馈左侧
   仍能看到「恢复之前关闭窗口」气泡，改到右侧）；
2) **DWM 隐身**（DWMWA_CLOAK）—— 比挪屏幕外彻底：窗口完全不渲染到屏幕，
   Alt+Tab / 任务栏 / 误点都找不出来，但 IsWindowVisible 仍为 TRUE、进程照常跑
   （不是最小化，不触发 document.hidden / rAF 节流）；
3) 隐身覆盖**全部**属于 socai Chrome 的窗口，包含气泡/弹出层 —— 恢复气泡那种
   带 owner 的 popup 曾是最初唯一漏网、唯一露在屏幕上的东西；
4) 启动前把 profile 标记为「正常退出」（_mark_clean_exit），从源头抑制
   Chrome 的「恢复之前关闭窗口」气泡。

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
import json
import os
import subprocess
import sys
import threading
import time

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "data", "logs", "browser_guard.log")

# socai 的 managed Chrome 一定带这个 profile 目录；用户日常的 Chrome 不带。
PROFILE_MARKS = (os.path.join(".socai", "chrome-profile"), ".socai/chrome-profile")
SOCAI_EXE = os.path.join(os.path.expanduser("~"), ".socai", "bin", "socai.exe")

# 扫描节奏：没发现浏览器时也不能太慢 —— Chrome 启动到显示窗口只有几百毫秒，
# 慢一拍就会让窗口在桌面上闪一下。psutil 只在进程名是 chrome.exe 时才去读
# cmdline，单次遍历约十几毫秒，1 秒一轮的代价可以忽略。
POLL_IDLE = 1.0
POLL_ACTIVE = 0.5
# 窗口消失多久算浏览器真关了
GONE_GRACE = 45
# 子进程（run.py）的兜底超时：正常一轮 35 分钟，给到 90 分钟
DEFAULT_CHILD_TIMEOUT = 90 * 60

user32 = ctypes.WinDLL("user32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:          # 老系统没有 dwmapi 也能跑，只是退回纯屏幕外停靠
    dwmapi = None

# DWM 隐身属性：cloak 后窗口**完全不渲染到屏幕**（比挪屏幕外更彻底，
# Alt+Tab、Win+方向键、误点任务栏都找不出来），但 IsWindowVisible 仍为 TRUE、
# 进程照常跑。不是最小化 —— 不触发 Chrome 的 document.hidden / rAF 节流路径。
DWMWA_CLOAK = 13

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
    """写 data/logs/browser_guard.log（guard 专属日志）。

    为什么不写 cron.log：daily.bat 用 `>> cron.log` 重定向 guard 的 stdout，
    这个句柄在整个 guard 进程期间都被 cmd 占着 —— 进程内任何 append 都会
    PermissionError，重试和「退出前回写」都没用（2026-09-29 实测丢 5 行）。
    所以 guard 落自己的文件，排查时两个日志都看：cron.log（流水线）+
    browser_guard.log（窗口隐藏/关闭）。
    """
    line = "[%s] browser_guard: %s" % (time.strftime("%Y/%m/%d %H:%M:%S"), msg)
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # 诊断日志不值得打断主流程


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


def _windows_of(pids: set[int], include_aux: bool = False) -> list[int]:
    """属于这些 PID 的顶层窗口。

    include_aux=False（主窗口）：排除弹出层与无标题辅助窗口 —— 只有它们是
        「用户可操作的那个浏览器窗口」。
    include_aux=True（全部）：**连弹出层/无标题气泡一起收**，
        2026-09-30 用户反馈「还是能看见『恢复之前关闭窗口』的弹窗」——
        那个恢复气泡是带 owner 的 popup，被上面的过滤规则漏掉了，只有它露在
        屏幕上。隐身必须覆盖全部窗口，过滤只用于「关窗口」这类需要挑主窗的场景。
    """
    found: list[int] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        if not include_aux:
            if user32.GetWindow(hwnd, GW_OWNER) != 0:  # 有 owner = 弹出层
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
        else:
            # 全部模式仍要排除零尺寸的隐形占位窗口，否则每轮刷一堆无意义日志
            left, top, w, h = _rect(hwnd)
            if w <= 1 or h <= 1:
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
    """整个虚拟桌面**右外侧**的一个点（多显示器也保证在可视区之外）。

    2026-09-29 原本放左侧；2026-09-30 用户反馈左侧仍能看到 Chrome 的
    「恢复之前关闭窗口」气泡（那个气泡按父窗口位置就近吸附，落到了最左边那块屏
    的边缘上）。改为右侧外侧 + 全面隐身（见 _windows_of 的 include_aux）。
    """
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    return vx + vw + 200, vy


def _cloak(hwnd: int, on: bool = True) -> bool:
    """DWM 隐身开关。返回是否成功。"""
    if dwmapi is None:
        return False
    val = ctypes.c_int(1 if on else 0)
    rc = dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), DWMWA_CLOAK,
                                      ctypes.byref(val), ctypes.sizeof(val))
    return rc == 0


def _cloaked(hwnd: int):
    """读取 DWM 隐身状态。真 / 假 / **None＝读不到**（接口不支持或调用失败）。

    注意：DWMWA_CLOAK 的读取在部分 Windows 版本上直接失败（2026-09-30 实测
    本机对已隐身的窗口读回 False）。所以这个值只能当参考，**不能**用来判断
    「要不要再隐一次」—— 那会让 park_all 永远认为没隐好。真正的做法是每次
    轮询都幂等地补一刀 _cloak(True)（DWM 调用很便宜）。
    """
    if dwmapi is None:
        return None
    val = ctypes.c_int(0)
    rc = dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), DWMWA_CLOAK,
                                      ctypes.byref(val), ctypes.sizeof(val))
    if rc != 0:
        return None
    return bool(val.value)


def _park(hwnd: int, x: int, y: int) -> bool:
    ok = bool(user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                  SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE))
    if ok:
        _cloak(hwnd, True)   # 隐身为主，屏幕外坐标只是兜底
        # 顺手摘掉任务栏按钮（失败无所谓，已经隐身了）
        try:
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            if new_ex != ex:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_ex)
                user32.ShowWindow(hwnd, SW_HIDE)
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                _cloak(hwnd, True)   # SW_HIDE/SHOW 会重置 cloak，补一次
        except OSError:
            pass
    return ok


def park_all(verbose: bool = True) -> int:
    """把所有 socai Chrome 窗口（含气泡/弹出层）挪出去 + 隐身。

    返回处理的窗口数。注意这里用 include_aux=True —— 「恢复之前关闭窗口」这类
    气泡必须一起收，否则它就是唯一露在屏幕上的东西（2026-09-30 用户实测反馈）。
    """
    pids = socai_chrome_pids()
    if not pids:
        return 0
    x, y = _offscreen_xy()
    n = 0
    for hwnd in _windows_of(set(pids), include_aux=True):
        left, top, w, h = _rect(hwnd)
        if abs(left - x) <= 8:
            # 位置已在屏外：**静默补一刀隐身**再走。原因：读取 cloak 状态的接口
            # 不可靠（见 _cloaked），而且 Chrome 在页面跳转/窗口重排时可能重置
            # 该属性 —— 幂等重设是唯一稳的写法，DWM 调用开销可忽略。
            _cloak(hwnd, True)
            continue
        if _park(hwnd, x, y):
            n += 1
            log("已把 socai Chrome 窗口移出屏幕：%r %dx%d @(%d,%d) -> (%d, %d)"
                % (_window_text(hwnd)[:40] or "<无标题/气泡>", w, h, left, top, x, y))
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
# 掐掉「恢复之前关闭窗口」气泡
# --------------------------------------------------------------------------
def _default_pref_path():
    return os.path.join(os.path.expanduser("~"), ".socai", "chrome-profile",
                        "Default", "Preferences")


def _mark_clean_exit(path: str = None) -> bool:
    """把 socai Chrome profile 标记为「上次正常退出」。

    为什么需要：guard 收尾时若 WM_CLOSE 超时，会降到 terminate()（强杀），
    Chrome 就把上次退出记为崩溃，**下次启动弹「恢复之前关闭窗口」气泡**。
    那个气泡是带 owner 的 popup，历史上不在我们的隐身名单里 —— 2026-09-30
    用户就是被它露出来的（主窗口已隐身，只有气泡可见）。实测确认当时 profile
    里就是 `exit_type: "Crashed"`。

    双保险：① 每次启动子进程前先把 profile 改回干净状态（气泡根本不出现）；
    ② 收尾后再写一次（让下一次启动也是干净的）。Chrome 在启动时会读这两个键
    决定是否弹恢复气泡。

    path 参数只为可测试（用临时文件跑单测），生产调用不传。
    """
    p = path or _default_pref_path()
    if not os.path.isfile(p):
        return False
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return False
    except (OSError, ValueError) as e:
        log("读 Chrome Preferences 失败（跳过）：%r" % (e,))
        return False

    prof = d.setdefault("profile", {})
    if prof.get("exit_type") == "Normal" and prof.get("exited_cleanly") is True:
        return False
    prof["exit_type"] = "Normal"
    prof["exited_cleanly"] = True
    d.pop("crashed", None)
    tmp = p + ".guard.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, p)
        log("已把 Chrome profile 标记为正常退出（抑制恢复气泡）")
        return True
    except OSError as e:
        log("写 Chrome Preferences 失败（跳过）：%r" % (e,))
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


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
    _mark_clean_exit()   # 先把 profile 标干净：Chrome 启动时不弹「恢复之前关闭窗口」
    stop = threading.Event()
    t = threading.Thread(target=_watch_loop, args=(stop,), daemon=True)
    t.start()
    log("守护启动：%s" % " ".join(child))

    rc = 1
    try:
        env = dict(os.environ, BROWSER_GUARD_ACTIVE="1")   # 子脚本用来判断自己有没有被包住
        proc = subprocess.Popen(child, cwd=ROOT, env=env)
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
        _mark_clean_exit()   # 收尾再写一次，下一次启动也不会弹恢复气泡

    log("流水线退出码 %d" % rc)
    return rc


def cmd_probe() -> int:
    pids = socai_chrome_pids()
    print("socai Chrome 进程：%s" % (pids or "无"))
    if pids:
        x, y = _offscreen_xy()
        print("目标屏外坐标（右侧外侧）：(%d, %d)" % (x, y))
        main = set(_windows_of(set(pids)))
        for hwnd in _windows_of(set(pids), include_aux=True):
            left, top, w, h = _rect(hwnd)
            print("  窗口 0x%X %-22r %dx%d @ (%d, %d)  屏外=%s  已隐身=%s  %s"
                  % (hwnd, _window_text(hwnd)[:20], w, h, left, top,
                     abs(left - x) <= 8, _cloaked(hwnd),
                     "主窗口" if hwnd in main else "气泡/弹出层"))
    all_chrome = [p.info["pid"] for p in psutil.process_iter(["pid", "name"])
                  if (p.info["name"] or "").lower() == "chrome.exe"]
    print("本机 chrome.exe 总数：%d（其中 socai 的 %d 个）"
          % (len(all_chrome), len(pids)))
    pref = os.path.join(os.path.expanduser("~"), ".socai", "chrome-profile",
                        "Default", "Preferences")
    try:
        with open(pref, encoding="utf-8") as f:
            d = json.load(f)
        pr = d.get("profile") or {}
        print("profile.exit_type=%r exited_cleanly=%r（Normal/True 才不弹恢复气泡）"
              % (pr.get("exit_type"), pr.get("exited_cleanly")))
    except (OSError, ValueError) as e:
        print("读 Preferences 失败：%r" % (e,))
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
