@echo off
REM Open the Career Intelligence reading page (latest issue) in a browser.
REM
REM Deliberately pure ASCII and BOM-free -- the same red line as daily.bat:
REM a UTF-8 BOM makes cmd fail on "@echo off" itself. Because this file contains
REM no non-ASCII text at all, it does NOT need daily.bat's chcp 65001 / re-exec
REM dance. If you ever add Chinese text here, you must copy that dance over.
REM
REM WHY NOT just:  start "" "%~dp0..\site\index.html"
REM   Because on this machine the .html association is broken:
REM       assoc .html -> htmlfile -> iexplore.exe
REM   and Internet Explorer no longer exists on Windows 11. "start" then fails
REM   SILENTLY -- exit code 0, no window, nothing in any log (verified 2026-09-18;
REM   PowerShell's Start-Process at least reported "cannot find the application",
REM   but cmd's start says nothing).
REM   So: probe for a real browser first, and only fall back to the association.
REM
REM Line endings must stay CRLF.

setlocal

set "PAGE=%~dp0..\docs\index.html"
if not exist "%PAGE%" (
    echo [open] page not found: "%PAGE%"
    echo        run:  python src\run.py --stage render
    exit /b 1
)

REM --- Locate a browser. NOTE: do NOT wrap this in a for(...) loop over
REM     "%ProgramFiles(x86)%" -- the parentheses inside that variable name close
REM     the for block early. Sequential single-line ifs are safe.
set "BROWSER="
if not defined BROWSER if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "BROWSER=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"

if defined BROWSER (
    start "" "%BROWSER%" "%PAGE%"
    exit /b 0
)

REM Last resort: hand it to the file association. On this machine that is the
REM broken iexplore.exe link, so this may do nothing -- but it is still the right
REM fallback on a machine that has a working default browser.
echo [open] no Chrome/Edge found; falling back to the .html association
start "" "%PAGE%"
exit /b 0
