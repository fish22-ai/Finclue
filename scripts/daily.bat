@echo off
REM Re-exec self under a 65001 console. Keep every line above ":utf8" ASCII-only.
REM cmd parses this file as it reads it, decoding with the console code page captured
REM at process start (936 on zh-CN). This file is UTF-8, so Chinese text below is
REM decoded as 936, split mid-character, and the fragments of REM lines then get
REM executed as commands -- that is the "'xxx' is not recognized" noise. A UTF-8 BOM
REM does not help: cmd ignores it and then fails on "@echo off" itself. The child cmd
REM starts with the console already at 65001 and reads this file consistently.
REM Same trap as dailybrief's scripts\daily.bat, diagnosed there 2026-09-15.
REM Do NOT use "shift" to drop the marker: shift also rewrites %0, and the
REM "cd /d %~dp0.." below relies on %0 to find the project root. In the child
REM process %1 is the marker, so the real arguments are in %2.
chcp 65001 >nul
if "%~1"=="_utf8" goto :utf8
cmd /d /c ""%~f0" _utf8 %*"
exit /b %errorlevel%
:utf8

REM ===========================================================================
REM  Career Intelligence 定时任务入口
REM  由 Windows 任务计划程序调用（scripts\install_task.ps1 注册，每 3 天 10:00）。
REM
REM  顺序：当日去重 → 预检 → 跑八段流水线 → 写 .last_success 哨兵
REM  任一步失败立即以非零退出，并把原因写进 data\logs\cron.log。
REM
REM  ⚠️ 诊断入口永远是这个 cron.log：run.py 的日志同时被重定向到这里，
REM     所以搜索、取正文、抽取、写照的每一步都在里面。
REM     手动强制重跑：scripts\daily.bat force
REM ===========================================================================

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
set "ROOT=%CD%"
set "LOG=%ROOT%\data\logs\cron.log"
if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"

echo. >> "%LOG%"
echo [%date% %time%] ===== 开始 ===== >> "%LOG%"

REM ---------- 当日去重 -------------------------------------------------------
REM  任务计划开了 StartWhenAvailable：到点没开机会在开机后补跑一次。补跑只是把
REM  「错过的那次」挪到现在执行，不是回头生成错过那天的那一期 —— run.py 不带
REM  --date 时取的是**当天**日期。万一同一天触发了两次（开机补偿 + 定时），
REM  这里把第二次挡掉，免得同一份数据被抓两遍、白烧一轮 LLM。
REM
REM  判据是 data\logs\.last_success（整条流水线跑完才写），而不是「data\raw 里
REM  有没有当天的文件」—— 产物写出来了但后面失败时，还得靠下一次运行续跑，
REM  用文件存在判断会把那次重试也一起挡掉。
set "TODAY="
for /f "usebackq delims=" %%d in (`python -c "import datetime;print(datetime.date.today().isoformat())"`) do set "TODAY=%%d"
REM  python 拿不到日期时不在这里报错 —— 下面的预检会给出更准确的提示。
if not defined TODAY goto :guard_done
REM  %1 是 _utf8 标记，真正的参数在 %2，所以两个都看。
if /i "%~1"=="force" goto :guard_done
if /i "%~2"=="force" goto :guard_done
set "LAST_SUCCESS="
if exist "%ROOT%\data\logs\.last_success" set /p LAST_SUCCESS=<"%ROOT%\data\logs\.last_success"
if /i "%LAST_SUCCESS%"=="%TODAY%" (
    echo [%date% %time%] 今日（%TODAY%）已成功跑过，跳过本次（强制重跑：scripts\daily.bat force） >> "%LOG%"
    exit /b 0
)
:guard_done

REM ---------- 预检 -----------------------------------------------------------
set "GIT_TERMINAL_PROMPT=0"
where python >nul 2>&1
if errorlevel 1 (
    echo [%date% %time%] 找不到 python，终止 >> "%LOG%"
    exit /b 1
)
if not exist "%ROOT%\src\run.py" (
    echo [%date% %time%] 找不到 src\run.py，终止 >> "%LOG%"
    exit /b 1
)
if not exist "C:\Users\吃鱿鱼的鱿鱼\.socai\bin\socai.exe" (
    echo [%date% %time%] 找不到 socai.exe，终止（路径见 config.yaml 的 binary） >> "%LOG%"
    exit /b 1
)

REM ---------- 跑流水线 -------------------------------------------------------
REM  run.py 自己会在「抓取结果为 0 条」时以非零退出（见 run.py 的守卫），
REM  所以 socai 登录态失效不会被当成静默成功。
python "%ROOT%\src\run.py" --stage all --source xhs >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] run.py 失败（非零退出）。常见原因： >> "%LOG%"
    echo     - socai 登录态失效：日志里搜「登录态失效」，需人工重新扫码 >> "%LOG%"
    echo     - socai daemon/CDP 异常：见 data\logs\%TODAY%.log >> "%LOG%"
    echo     - LLM 上游不可用：搜「LLM 调用最终失败」 >> "%LOG%"
    echo     - 候选池空了且搜索失败：搜「没有未抓过的卡片」 >> "%LOG%"
    echo     - 站点没更新：搜「渲染失败」。注意渲染是**非致命**的， >> "%LOG%"
    echo       它不会让本行触发；下次运行会重渲所有期，自己会补上 >> "%LOG%"
    exit /b 1
)

> "%ROOT%\data\logs\.last_success" echo %TODAY%
echo [%date% %time%] ===== 完成 ===== >> "%LOG%"
exit /b 0
