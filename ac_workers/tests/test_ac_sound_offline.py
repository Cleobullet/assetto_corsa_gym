"""Offline tests for C4D sound on/off."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_sound_command_usage_and_status():
    from ac_workers import ac_sound

    orig_apply = ac_sound.apply_sound
    orig_pref = ac_sound.PREF_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            ac_sound.PREF_PATH = Path(td) / "sound.json"
            ac_sound.apply_sound = lambda enabled: "applied {0}".format(enabled)
            assert "sound on" in ac_sound.handle_sound_command("")
            assert ac_sound.handle_sound_command("on") == "applied True"
            assert ac_sound.handle_sound_command("off") == "applied False"
            assert ac_sound.handle_sound_command("nope").startswith("usage:")
    finally:
        ac_sound.apply_sound = orig_apply
        ac_sound.PREF_PATH = orig_pref


def test_audio_ini_master_toggle():
    from ac_workers import ac_sound

    orig = ac_sound._documents_audio_ini
    try:
        with tempfile.TemporaryDirectory() as td:
            ini = Path(td) / "audio.ini"
            ini.write_text("[LEVELS]\nMASTER=1\nENGINE=1\n", encoding="utf-8")
            ac_sound._documents_audio_ini = lambda: ini
            assert "MASTER=0" in ac_sound.set_audio_ini_master(False)
            assert "MASTER=0" in ini.read_text(encoding="utf-8")
            assert "MASTER=1" in ac_sound.set_audio_ini_master(True)
            assert "MASTER=1" in ini.read_text(encoding="utf-8")
    finally:
        ac_sound._documents_audio_ini = orig


def test_control_center_sound_command():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    with mock.patch("ac_workers.ac_sound.handle_sound_command", return_value="sound on") as fn:
        assert center.handle_command("volume off") == "sound on"
        fn.assert_called_once_with("off")
    help_text = center.handle_command("help")
    assert "sound" in help_text


def main() -> int:
    tests = [
        test_sound_command_usage_and_status,
        test_audio_ini_master_toggle,
        test_control_center_sound_command,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-AcSound-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
