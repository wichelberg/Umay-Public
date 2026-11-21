# Builds dist\Umay.exe with PyInstaller (Windows, Python with requirements + pyinstaller installed).
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The python on PATH: the one README's `python -m pip install -r requirements.txt` filled. (The py launcher
# picks the newest Python 3, which may not have them: 3.13 here while they are in 3.11.)
# (@(...) around the if: a one-item array coming out of an if becomes a plain string, and $python[0] "p".)
# A shell started by a running Umay inherits TCL_LIBRARY/TK_LIBRARY pointing into Umay's own _MEI folder. Once that
# folder is gone PyInstaller finds Tcl/Tk "broken" and builds an exe without tkinter (2026-11-20: "No module named
# 'tkinter'" at start-up), so drop them.
Remove-Item Env:TCL_LIBRARY, Env:TK_LIBRARY -ErrorAction SilentlyContinue
$python = @(if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "py", "-3" })
& $python[0] $python[1..9] -m PyInstaller --noconfirm --onefile --windowed --name Umay --icon assets\umay.ico `
    --add-data "assets;assets" --add-data "sounds;sounds" --add-data "personality.md;." --add-data "cortex.html;." --add-data "voice_worker.py;." --add-data "speech_worker.py;." --add-data "tts_worker.py;." --add-data "piper_worker.py;." --add-data "hands_worker.py;." --add-data "hand_gesture.py;." --add-data "face_worker.py;." --add-data "sleep_hook.ps1;." `
    --hidden-import email.policy --hidden-import email.message --hidden-import email.utils `
    --hidden-import imaplib --hidden-import smtplib --hidden-import html --hidden-import mimetypes `
    --hidden-import secrets --hidden-import argparse --hidden-import urllib.request `
    --hidden-import send2trash --hidden-import yt_dlp --hidden-import websockets.sync.server `
    --collect-submodules holidays --collect-data holidays `
    --collect-submodules ddgs --collect-all trafilatura --collect-all justext --collect-all courlan --collect-all htmldate --collect-all dateparser `
    main.py

# UmayLights.exe (lights_worker.py): the keyboard light's helper, with the msix manifest that ties it to its sparse
# package (packaging/lights). It goes to Apps\Umay\lights\UmayLights.exe: that folder is the package's external location.
& $python[0] $python[1..9] -m PyInstaller --noconfirm --onefile --windowed --name UmayLights `
    --manifest packaging\lights\UmayLights.exe.manifest `
    --hidden-import winrt.windows.devices.lights --hidden-import winrt.windows.devices.enumeration --hidden-import winrt.windows.ui `
    --hidden-import winrt.windows.foundation --hidden-import winrt.windows.foundation.collections `
    lights_worker.py

Write-Host "Built dist\Umay.exe and dist\UmayLights.exe. Put .env (and optionally personality.md, config.json) next to Umay.exe; UmayLights.exe goes to Apps\Umay\lights."
