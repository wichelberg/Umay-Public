import os
import sys
from pathlib import Path

LEGACY_ENV_PREFIX = "HANNAH_"  # the app's former name; old .env files keep working
ENV_PREFIX = "UMAY_"


def app_dir() -> Path:
    """Directory the exe lives in when frozen, else this project's directory.

    Used for user-editable/persistent files (config.json, .env, chat_logs)
    so they survive next to the exe instead of PyInstaller's temp extraction dir.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Directory holding bundled read-only assets (images, sounds).

    PyInstaller (onefile) extracts --add-data assets into sys._MEIPASS at
    runtime, not next to the exe, so bundled assets must be read from there.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent


def voice_home() -> Path:
    """Local speech files: voice_env (env/), the Whisper model (models/) and recorded
    training samples (data/). Outside the app folder so the exe and a source checkout
    share them."""
    custom = os.environ.get("UMAY_VOICE_HOME", "").strip()
    return Path(custom) if custom else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay" / "voice"


# Keys that came from Umay's .env (not from the real environment). They're Umay's settings,
# so programs she starts (Claude Code) don't get them: e.g. an ANTHROPIC_API_KEY there would make
# Claude Code bill that key instead of the user's own login.
ENV_FILE_KEYS = set()


def load_env(path=None):
    """Loads KEY=VALUE lines from .env next to the app into os.environ (real
    environment variables win). Runs when this module is first imported, so
    every module that reads settings at import time already sees them."""
    path = Path(path or app_dir() / ".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in os.environ:
                os.environ[key] = value.strip()
                ENV_FILE_KEYS.add(key)
    for key, value in list(os.environ.items()):
        if key.startswith(LEGACY_ENV_PREFIX):
            os.environ.setdefault(ENV_PREFIX + key[len(LEGACY_ENV_PREFIX):], value)


def drop_editor_env():
    """Removes what VS Code sets for its extension host. Umay inherits it when a Claude Code
    session in VS Code starts her (e.g. after a rebuild), and every program she launches would get
    it too: ELECTRON_RUN_AS_NODE makes VS Code (and any Electron app) run as plain Node, treat the
    folder as a script and quit without a window; VSCODE_* point it at the editor that started her."""
    for key in list(os.environ):
        if key.upper() == "ELECTRON_RUN_AS_NODE" or key.upper().startswith("VSCODE_"):
            del os.environ[key]


def take_bootloader_env():
    """Takes PyInstaller's _PYI_* markers (where the running Umay.exe is unpacked) out of os.environ, so
    programs Umay starts don't inherit them. They did (2026-11-04): Umay opened a Claude Code session, that
    session restarted Umay after a rebuild, and the new Umay.exe saw _PYI_APPLICATION_HOME_DIR / _PYI_PARENT_
    PROCESS_LEVEL, took itself for a child of the old one and ran the OLD unpacked files (no winrt: Spotify
    said "No module named 'winrt._winrt'") instead of unpacking itself. Only Umay's own helper (the HUD,
    sys.executable --hud) gets them back: own_child_env()."""
    return {k: os.environ.pop(k) for k in list(os.environ) if k.upper().startswith("_PYI_")}


def own_child_env():
    """The environment for starting this same Umay.exe as a helper: it shares this process's unpacked
    files (PyInstaller's way), so the markers come back."""
    return dict(os.environ, **BOOTLOADER_ENV)


drop_editor_env()
BOOTLOADER_ENV = take_bootloader_env()
load_env()
