# Builds dist\Umay.exe with PyInstaller (Windows, Python with requirements + pyinstaller installed).
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The python on PATH: the one README's `python -m pip install -r requirements.txt` filled. (The py launcher
# picks the newest Python 3, which may not have them: 3.13 here while they are in 3.11.)
# (@(...) around the if: a one-item array coming out of an if becomes a plain string, and $python[0] "p".)
$python = @(if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "py", "-3" })
& $python[0] $python[1..9] -m PyInstaller --noconfirm --onefile --windowed --name Umay --icon assets\umay.ico `
    --add-data "assets;assets" --add-data "sounds;sounds" --add-data "personality.md;." --add-data "cortex.html;." --add-data "voice_worker.py;." --add-data "speech_worker.py;." --add-data "tts_worker.py;." --add-data "piper_worker.py;." --add-data "hands_worker.py;." --add-data "hand_gesture.py;." --add-data "face_worker.py;." --add-data "sleep_hook.ps1;." `
    --hidden-import email.policy --hidden-import email.message --hidden-import email.utils `
    --hidden-import imaplib --hidden-import smtplib --hidden-import html --hidden-import mimetypes `
    --hidden-import secrets --hidden-import argparse --hidden-import urllib.request `
    --hidden-import send2trash --hidden-import yt_dlp --hidden-import websockets.sync.server `
    --collect-submodules holidays --collect-data holidays `
    main.py

Write-Host "Built dist\Umay.exe. Put .env (and optionally personality.md, config.json) next to it."
