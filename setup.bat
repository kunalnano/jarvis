@echo off
echo Setting up Jarvis AI Assistant...

cd /d "%~dp0"

:: Create virtual environment
python -m venv .venv
call .venv\Scripts\activate

:: Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

:: Copy config if not exists
if not exist config\jarvis.yaml (
    copy config\jarvis.windows.yaml config\jarvis.yaml
    echo.
    echo WARNING: Config created at config\jarvis.yaml
    echo          Edit it to add your ElevenLabs API key and voice ID
)

echo.
echo Setup complete!
echo.
echo Next steps:
echo 1. Edit config\jarvis.yaml with your ElevenLabs settings
echo 2. Start LM Studio and load a model
echo 3. Run: start_jarvis.bat

pause
