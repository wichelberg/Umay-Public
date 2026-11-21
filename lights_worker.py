"""UmayLights: the laptop keyboard's light in Umay's state color (the owner's wish, 2026-11-21).

Its own small exe (UmayLights.exe), not Umay.exe: Windows gives Dynamic Lighting devices (LampArray) to the app in front, or
in the background only to an app with package identity that declares the "com.microsoft.windows.lighting" app extension and
that the owner picked under Settings > Personalization > Dynamic Lighting > Background light control. That identity comes
from a sparse package (packaging/lights: AppxManifest.xml with an external location, the exe's msix manifest). Umay starts
Claude sessions, voice workers and more; inside a package context they could all behave differently, so only this small
process gets the identity.

Protocol: one line per change on stdin, a hex color ("FF969D") or "off" (the keyboard back to Windows' own lighting).
EOF (Umay closed) ends it, and Windows gives the light back. Only the GIGABYTE keyboard (vendor 0x414): G HUB owns the mouse.
--hold HEX --seconds N: hold one color and log the availability every 2 s (the first test of a new install).
Log: %LOCALAPPDATA%\\Umay\\lights.log.
"""
import asyncio
import ctypes
import os
import sys
import threading
import time
from pathlib import Path

VENDOR = 0x414
APPMODEL_ERROR_NO_PACKAGE = 15700


def _log(text):
    try:
        home = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Umay"
        home.mkdir(parents=True, exist_ok=True)
        with open(home / "lights.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def package_identity():
    """The package's full name when this process has package identity, else ""."""
    length = ctypes.c_uint32(0)
    rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
    if rc == APPMODEL_ERROR_NO_PACKAGE:
        return ""
    buf = ctypes.create_unicode_buffer(length.value or 256)
    rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), buf)
    return buf.value if rc == 0 else ""


def parse(line):
    """"FF969D" / "#ff969d" -> (r, g, b); "off" -> None; anything else -> False (ignored)."""
    text = (line or "").strip().lstrip("#").lower()
    if text == "off":
        return None
    if len(text) == 6:
        try:
            return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return False
    return False


async def keyboard():
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.devices.lights import LampArray
    for info in await DeviceInformation.find_all_async_aqs_filter(LampArray.get_device_selector()):
        lamps = await LampArray.from_id_async(info.id)
        if lamps is not None and lamps.hardware_vendor_id == VENDOR:
            return lamps
    return None


def paint(lamps, rgb):
    from winrt.windows.ui import Color
    if rgb is None:
        lamps.set_color(Color(0, 0, 0, 0))   # transparent: the ambient effect shows through
        return
    lamps.set_color(Color(255, *rgb))


async def hold(hexcolor, seconds):
    lamps = await keyboard()
    _log(f"hold test: identity={package_identity() or 'none'} keyboard={'yes' if lamps else 'no'}")
    if lamps is None:
        return
    rgb = parse(hexcolor) or (255, 150, 157)
    end = time.time() + seconds
    while time.time() < end:
        paint(lamps, rgb)
        _log(f"hold test: available={lamps.is_available}")
        await asyncio.sleep(2)


async def serve():
    lamps = await keyboard()
    _log(f"start: identity={package_identity() or 'none'} keyboard={'yes' if lamps else 'no'}")
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()

    def read():
        for line in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, None)   # EOF: Umay is gone
    threading.Thread(target=read, daemon=True).start()
    current = False
    if lamps is not None:
        # when Windows gives the light back (another app let go, or this one was picked), paint it again
        lamps.add_availability_changed(lambda sender, args: loop.call_soon_threadsafe(
            lambda: current is not False and lamps.is_available and paint(lamps, current)))
    while True:
        line = await lines.get()
        if line is None:
            break
        rgb = parse(line)
        if rgb is False:
            continue
        current = rgb
        if lamps is not None:
            try:
                paint(lamps, rgb)
            except Exception as e:  # a light must never matter
                _log(f"paint failed: {type(e).__name__}: {e}")
    _log("stdin closed: stop")


def main(argv):
    try:
        if "--hold" in argv:
            i = argv.index("--hold")
            seconds = float(argv[argv.index("--seconds") + 1]) if "--seconds" in argv else 120
            asyncio.run(hold(argv[i + 1] if len(argv) > i + 1 else "FF969D", seconds))
        else:
            asyncio.run(serve())
    except Exception as e:
        _log(f"error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
