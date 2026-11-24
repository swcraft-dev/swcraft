@echo off
rem Install the SWCraft toolchain from this unpacked archive.
rem
rem The archive carries its own Python; this file gets install.py one to run on:
rem   1. hash runtime/python.tar.gz with certutil and refuse in one sentence when
rem      no line of SHA256SUMS carries that digest for that member;
rem   2. unpack it into .bootstrap\python beside this file, dropping the vendor's
rem      one leading directory;
rem   3. run install.py on that interpreter, passing every argument through.
rem
rem install.py cannot remove .bootstrap itself (Windows keeps the running
rem python.exe open), so this file removes it once install.py has returned.
rem
rem certutil's digest is its one line without a colon; an older Windows puts spaces
rem between the bytes, dropped before the search. tar.exe is named by its full path:
rem a GNU tar earlier on PATH (Git for Windows) reads a drive letter as a host name.
rem
rem "preparing the installer ... " is closed by "done" or by "failed" before the
rem refusal on stderr, so the refusal stays the last line. The open start
rem (`<nul set /p`) stands before certutil because `set /p` with no input sets
rem errorlevel to 1, so it must not stand between a command and its `if errorlevel`.
rem Not measured: the open line rendered live, and the double-click branch below.
rem
rem No labels and no goto: the tree is LF-only, and a label reached by goto is
rem the documented failure mode of an LF-only .cmd.
rem
rem No parenthesised block either:
rem `%errorlevel%` inside one is expanded when the block is parsed, before its
rem command runs, which reported every refusal as success. Line by line, each
rem `%errorlevel%` reads the line before it, with no delayed expansion (so a `!` in
rem a path is not mangled); each act after a refusal checks SWCRAFT_REFUSAL.
setlocal
set "SWCRAFT_BOOT=%~dp0.bootstrap"
set "SWCRAFT_PY=%~dp0.bootstrap\python\python.exe"
set "SWCRAFT_REFUSAL="
set "SWCRAFT_DIGEST="
if not exist "%SWCRAFT_BOOT%" mkdir "%SWCRAFT_BOOT%"
<nul set /p "=preparing the installer ... "
certutil -hashfile "%~dp0runtime\python.tar.gz" SHA256 | findstr /v ":" > "%SWCRAFT_BOOT%\python.sha256"
set /p SWCRAFT_DIGEST=<"%SWCRAFT_BOOT%\python.sha256"
del "%SWCRAFT_BOOT%\python.sha256" >nul 2>&1
if defined SWCRAFT_DIGEST set "SWCRAFT_DIGEST=%SWCRAFT_DIGEST: =%"
findstr /b /i /c:"%SWCRAFT_DIGEST%  runtime/python.tar.gz" "%~dp0SHA256SUMS" >nul 2>&1
if errorlevel 1 set "SWCRAFT_REFUSAL=runtime\python.tar.gz does not match its line in SHA256SUMS, so nothing was installed: download the archive again"
if not defined SWCRAFT_REFUSAL if exist "%SWCRAFT_BOOT%\python" rmdir /s /q "%SWCRAFT_BOOT%\python"
if not defined SWCRAFT_REFUSAL mkdir "%SWCRAFT_BOOT%\python"
if not defined SWCRAFT_REFUSAL "%SystemRoot%\System32\tar.exe" -xzf "%~dp0runtime\python.tar.gz" -C "%SWCRAFT_BOOT%\python" --strip-components 1
if not defined SWCRAFT_REFUSAL if errorlevel 1 set "SWCRAFT_REFUSAL=runtime\python.tar.gz could not be unpacked into .bootstrap\python, so nothing was installed: unpack the archive again"
if defined SWCRAFT_REFUSAL echo failed
if defined SWCRAFT_REFUSAL echo %SWCRAFT_REFUSAL% 1>&2
if not defined SWCRAFT_REFUSAL echo done
if not defined SWCRAFT_REFUSAL "%SWCRAFT_PY%" "%~dp0install.py" %*
set "SWCRAFT_EXIT=%errorlevel%"
if defined SWCRAFT_REFUSAL set "SWCRAFT_EXIT=1"
rem A refusal leaves no .bootstrap it made: rmdir without /s removes only an
rem empty directory, so an earlier or half-unpacked copy is left for the next run.
if defined SWCRAFT_REFUSAL rmdir "%SWCRAFT_BOOT%" >nul 2>&1
rem Whatever install.py answered, nothing uses the copy now; its removal failing
rem is not the installer's failure, so the saved code is still the exit code.
if not defined SWCRAFT_REFUSAL rmdir /s /q "%SWCRAFT_BOOT%" >nul 2>&1
rem Double-clicked in Explorer, the window closes the instant this ends, so wait
rem for a key. `%cmdcmdline%` names this file for ANY `cmd.exe /c <wrapper>` launch
rem (PowerShell, a build agent, where `pause` would hang), so three conditions:
rem the command line names this file, no argument was passed, and SWCRAFT_NO_PAUSE
rem is unset. Not reached: an interactive PowerShell run with no arguments waits
rem for a key too. The probe overwrites %errorlevel%, hence the saved code above.
rem Unmeasured: no double-click launch has been observed.
set "SWCRAFT_PAUSE="
echo %cmdcmdline% | find /i "%~0" >nul
if not errorlevel 1 if "%~1"=="" if not defined SWCRAFT_NO_PAUSE set "SWCRAFT_PAUSE=1"
if defined SWCRAFT_PAUSE pause
exit /b %SWCRAFT_EXIT%
