@echo off
setlocal
if defined MUSIC_TAG_PYTHON (
  "%MUSIC_TAG_PYTHON%" "%~dp0music_tag.py" %*
  exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0music_tag.py" %*
  exit /b
)
python "%~dp0music_tag.py" %*
