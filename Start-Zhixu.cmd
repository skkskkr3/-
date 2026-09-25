@echo off
setlocal
pushd "%~dp0" || goto :path_error
where powershell.exe >nul 2>nul || goto :powershell_error
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
set "ZHIXU_EXIT=%ERRORLEVEL%"
if not "%ZHIXU_EXIT%"=="0" (
  echo.
  echo Zhixu failed to start. Error code: %ZHIXU_EXIT%
  echo Keep this window open and send a screenshot of the error.
  pause
)
popd
exit /b %ZHIXU_EXIT%

:path_error
echo Cannot open the Zhixu folder. Extract the whole ZIP before starting.
pause
exit /b 2

:powershell_error
echo Windows PowerShell was not found.
pause
popd
exit /b 3
