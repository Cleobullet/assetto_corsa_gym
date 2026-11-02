"""C4D game-sound toggle: audio.ini MASTER + focus/unfocus AC windows."""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PREF_PATH = REPO / "ac_workers" / "status" / "sound.json"

SW_RESTORE = 9


def _documents_audio_ini() -> Path | None:
    env_path = os.environ.get("AC_DOCUMENTS_PATH")
    candidates = []
    if env_path:
        candidates.append(Path(env_path) / "cfg" / "audio.ini")
    home = Path.home()
    candidates.append(home / "Documents" / "Assetto Corsa" / "cfg" / "audio.ini")
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        candidates.append(Path(onedrive) / "Documents" / "Assetto Corsa" / "cfg" / "audio.ini")
    for path in candidates:
        if path.is_file():
            return path
    return None


def read_pref() -> dict[str, Any]:
    try:
        return json.loads(PREF_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": True}


def write_pref(enabled: bool) -> None:
    PREF_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREF_PATH.write_text(
        json.dumps({"enabled": bool(enabled)}, indent=2),
        encoding="utf-8",
    )


def set_audio_ini_master(enabled: bool) -> str:
    path = _documents_audio_ini()
    if path is None:
        return "audio.ini introuvable"
    text = path.read_text(encoding="utf-8-sig")
    target = "1" if enabled else "0"
    lines = text.splitlines(True)
    changed = False
    out: list[str] = []
    in_levels = False
    for line in lines:
        strip = line.strip()
        if strip.startswith("[") and strip.endswith("]"):
            in_levels = strip.upper() == "[LEVELS]"
        if in_levels and strip.upper().startswith("MASTER="):
            nl = "\n" if line.endswith("\n") else ""
            line = "MASTER=" + target + nl
            changed = True
        out.append(line)
    if changed:
        path.write_text("".join(out), encoding="utf-8")
        return "audio.ini MASTER={0}".format(target)
    return "audio.ini MASTER inchange"


def _acs_hwnds() -> list[int]:
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        title = (buf.value or "").lower()
        if "assetto corsa" in title:
            found.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return found


def focus_ac_window() -> str:
    user32 = ctypes.windll.user32
    hwnds = _acs_hwnds()
    if not hwnds:
        return "aucune fenetre AC"
    hwnd = hwnds[0]
    user32.ShowWindow(hwnd, SW_RESTORE)
    ok = bool(user32.SetForegroundWindow(hwnd))
    return "fenetre AC activee" if ok else "fenetre AC trouvee (clique-la si tu n'entends rien)"


def focus_console_window() -> str:
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    hwnd = int(kernel32.GetConsoleWindow() or 0)
    if hwnd <= 0:
        return "console C4D"
    user32.SetForegroundWindow(hwnd)
    return "son coupe (fenetre AC en arriere-plan)"


def apply_sound(enabled: bool) -> str:
    write_pref(enabled)
    ini = set_audio_ini_master(enabled)
    if enabled:
        focus = focus_ac_window()
        return "son allume. {0}. {1}".format(ini, focus)
    focus = focus_console_window()
    return "son coupe. {0}. {1}".format(ini, focus)


def sound_status() -> str:
    pref = read_pref()
    enabled = bool(pref.get("enabled", True))
    return "sound {0}  (sound on | sound off)".format("on" if enabled else "off")


def handle_sound_command(arg: str) -> str:
    token = (arg or "").strip().lower()
    if token in ("", "status"):
        return sound_status()
    if token in ("on", "1", "unmute", "allume", "oui"):
        return apply_sound(True)
    if token in ("off", "0", "mute", "coupe", "non"):
        return apply_sound(False)
    return "usage: sound | sound on | sound off"
