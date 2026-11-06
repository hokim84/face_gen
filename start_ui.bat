@echo off
rem NPC 초상화 생성기 웹 UI 실행 (더블클릭)
chcp 65001 >nul
cd /d "%~dp0"

set PY=python
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe

%PY% -c "import fastapi, uvicorn, PIL" 2>nul
if errorlevel 1 (
    echo 웹 UI 에 필요한 패키지를 설치합니다...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [!] 패키지 설치에 실패했습니다. 위 오류 메시지를 확인하세요.
        pause
        exit /b 1
    )
)

%PY% server.py %*
if errorlevel 1 pause
