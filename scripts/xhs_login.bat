@echo off
REM Re-exec self under a 65001 console. Keep every line above ":utf8" ASCII-only.
REM (Same codepage trap as daily.bat: this file is UTF-8, zh-CN console decodes
REM  it as 936 and fragments of REM lines get executed as commands. The child
REM  cmd starts at 65001 and reads the file consistently.)
REM Do NOT use "shift": the "cd /d %~dp0.." below relies on %0 to find the root.
chcp 65001 >nul
if "%~1"=="_utf8" goto :utf8
cmd /d /c ""%~f0" _utf8 %*"
exit /b %errorlevel%
:utf8

REM ===========================================================================
REM  小红书手动扫码登录入口 —— 唯一需要人参与的环节，由你主动双击触发。
REM
REM  背景：socai 的登录态隔一段时间会失效，定时抓取发现自己登不上时
REM  **不会**弹 Chrome 窗口（那会在面试/共享屏幕时社死），只发一条系统通知。
REM  你看到通知后，在方便的时候双击这个文件：
REM    1) 会弹出一个正常的 Chrome 窗口显示小红书登录二维码
REM    2) 用手机小红书 App 扫码确认
REM    3) 命令自己会跑完并提示成功，然后关掉窗口即可
REM
REM  平时登录态正常时跑这个文件也无害 —— 只是做一次轻量搜索（几秒）。
REM ===========================================================================

chcp 65001 >nul
cd /d "%~dp0.."
echo.
echo  正在打开小红书，如果登录已失效，弹出的 Chrome 里会出现二维码...
echo  请用手机小红书 App 扫码登录。登录成功后这个命令会自己结束。
echo.
"C:\Users\吃鱿鱼的鱿鱼\.socai\bin\socai.exe" xhs search "测试" --preview --num-notes 1
REM 登录标记（data\logs\login_needed.txt）：用户主动来扫码了就清掉它，
REM 免得下次还提示；万一没扫成功，下一轮抓取失败时会重新写。
if exist "data\logs\login_needed.txt" del "data\logs\login_needed.txt" >nul 2>&1
echo.
echo  命令已结束。如果上面输出了搜索结果卡片，说明登录态正常；
echo  现在可以关掉 Chrome 窗口和本窗口了。
pause
