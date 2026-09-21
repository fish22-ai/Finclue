@echo off
REM Serve docs/ over local HTTP so the PWA is installable from a browser
REM (manifest + service worker need a secure context; file:// will not do).
REM Pure ASCII, CRLF endings -- same red line as open.bat / daily.bat.
REM
REM Usage: scripts\serve.bat   -> http://localhost:8802/

setlocal

set "PORT=8802"
set "DIR=%~dp0..\docs"

REM Open the browser first (async), then block on the server. Ctrl+C stops it.
start "" "http://localhost:%PORT%/"

python -m http.server %PORT% --directory "%DIR%"
endlocal
