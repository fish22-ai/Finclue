' scripts\run_hidden.vbs -- launch a console script with NO visible window.
'
' Why this exists (2026-09-20): the Task Scheduler action used to be
'     cmd.exe /d /c "...\daily.bat"
' with LogonType=Interactive, so Windows allocated a real console inside the
' user's session and a black window sat on screen for the entire run (minutes).
' The window carried no information -- every step already redirects into
' data\logs\cron.log -- and it showed up at the worst possible moment
' (mid-interview).
'
' How it hides: wscript.exe is a GUI-subsystem binary, so it never allocates a
' console of its own. It then starts cmd via Run(cmd, 0, True): window style 0
' puts SW_HIDE in the child's STARTUPINFO, so the console is created hidden
' rather than hidden after the fact -- that is what makes it flash-free.
' daily.bat re-execs itself with "cmd /d /c" to get a 65001 console; that
' grandchild inherits the same hidden console, so the whole process tree stays
' invisible from trigger to exit.
'
' The third argument to Run MUST stay True (wait). With False this script would
' return immediately: Task Scheduler would mark the task "succeeded" in
' milliseconds while the real work was still running, which would silently break
' MultipleInstances=IgnoreNew (the re-entrancy guard) and the 2h
' ExecutionTimeLimit, and LastTaskResult would always read 0.
'
' The exit code is propagated, so LastTaskResult still reflects real
' success/failure exactly as it did under cmd.exe.
'
' Same file as dailybrief's scripts\run_hidden.vbs -- keep the two in sync.
'
' Keep this file pure ASCII, CRLF, no BOM: WSH reads .vbs as ANSI, so a BOM or
' any multibyte text would be misread, and LF-only endings are not reliably
' parsed. .gitattributes pins eol=crlf for the same reason.
'
' Usage (same as the old action; extra args are passed through):
'     wscript.exe //B "scripts\run_hidden.vbs" "scripts\daily.bat" [force]

Option Explicit

Dim sh, cmd, i, rc

If WScript.Arguments.Count = 0 Then
    WScript.Quit 87    ' ERROR_INVALID_PARAMETER: nothing to launch
End If

' Quote every argument itself, so paths containing spaces survive -- and so does
' the Chinese repo path, which would otherwise be split or mis-decoded.
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
    If i > 0 Then cmd = cmd & " "
    cmd = cmd & """" & WScript.Arguments(i) & """"
Next

Set sh = CreateObject("WScript.Shell")
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
