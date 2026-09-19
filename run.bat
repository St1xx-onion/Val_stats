@echo off
setlocal enabledelayedexpansion
title Valorant Stats
chcp 65001 >nul
rem Riot IDs come in every script there is; keep Python's output UTF-8 too.
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%~dp0"

rem ---------------------------------------------------------------- find python
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY goto :nopython

set "VENV=.venv\Scripts\python.exe"

rem ------------------------------------------------------------- first-run setup
if not exist "%VENV%" (
    echo First run - setting up. This takes about a minute.
    echo.
    %PY% -m venv .venv
    if errorlevel 1 goto :venvfail
    "%VENV%" -m pip install --upgrade pip --quiet
    "%VENV%" -m pip install -r requirements.txt --quiet
    if errorlevel 1 goto :depsfail
    echo Setup done.
    echo.
)

if not exist "config.json" (
    copy /y "config.example.json" "config.json" >nul
    echo Created config.json - edit it if you want to change anything.
    echo.
)

rem ------------------------------------------------------------------- dispatch
if /i "%~1"=="test"  goto :selftest
if /i "%~1"=="check" goto :offlinetests
if /i "%~1"=="reset" goto :reset
if /i "%~1"=="who"         goto :passthrough
if /i "%~1"=="mates"       goto :passthrough
if /i "%~1"=="with"        goto :passthrough
if /i "%~1"=="identify"    goto :passthrough
if /i "%~1"=="id"          goto :passthrough
if /i "%~1"=="match"       goto :passthrough
if /i "%~1"=="top"         goto :passthrough
if /i "%~1"=="backfill"    goto :passthrough
if /i "%~1"=="agents"      goto :passthrough
if /i "%~1"=="calibration" goto :passthrough
if /i "%~1"=="calib"       goto :passthrough
if /i "%~1"=="share"       goto :passthrough
if /i "%~1"=="pool"        goto :passthrough
if /i "%~1"=="update"      goto :passthrough
if /i "%~1"=="upgrade"     goto :passthrough
if not "%~1"=="" goto :usage

"%VENV%" -m valstats
goto :done

:passthrough
"%VENV%" -m valstats %*
goto :done

:selftest
"%VENV%" -m valstats.selftest
goto :done

:offlinetests
"%VENV%" -m tests.test_stats
goto :done

:reset
set "STUCK="
if exist "encounters.db" del /q "encounters.db" >nul 2>&1
if exist "encounters.db" set "STUCK=1"
if exist "cache" rmdir /s /q "cache" >nul 2>&1
if exist "cache" set "STUCK=1"
if defined STUCK goto :resetstuck
echo Match memory and content cache cleared.
goto :done

:resetstuck
echo.
echo Could not clear everything - the files are still in use.
echo Close any running Valorant Stats window, then run this again.
goto :done

rem --------------------------------------------------------------------- errors
:nopython
echo.
echo Python was not found on PATH.
echo Install Python 3.10 or newer from python.org and tick
echo "Add python.exe to PATH" in the installer.
goto :done

:venvfail
echo.
echo Could not create the virtual environment in .venv
echo Try deleting the .venv folder and running this again.
goto :done

:depsfail
echo.
echo Could not install dependencies. Check your internet connection,
echo then delete the .venv folder and run this again.
goto :done

:usage
echo.
echo Usage:
echo   run.bat               watch for matches (default)
echo   run.bat test          check the whole chain against the running client
echo   run.bat check         run the offline parsing tests
echo   run.bat reset         clear the local match memory and content cache
echo.
echo   run.bat who ^<name^>    what the local memory knows about a player
echo   run.bat mates ^<name^>  who that player keeps queueing with
echo   run.bat identify      name the players who hid behind streamer mode
echo   run.bat match ^<M12^>   the Riot match id behind a local match number
echo   run.bat top [n]       the people you run into most often
echo   run.bat backfill [n]  parse your own recent matches into the cache
echo   run.bat agents [n]    your own agent pool, as the pick advice sees it
echo   run.bat calibration   what the 0-1000 score is measured against
echo   run.bat share         the shared match pool: status, "share now" to send
echo   run.bat pool          the downloaded pool: status, "pool sync" to fetch
echo   run.bat update        check GitHub for a newer version and install it
goto :done

:done
echo.
pause
