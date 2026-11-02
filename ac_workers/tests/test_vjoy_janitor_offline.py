"""Janitor heartbeat must parse when PowerShell writes UTF-8 BOM."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_heartbeat_with_bom_is_alive(tmp_path):
    import ac_workers.vjoy_janitor as jan

    hb = tmp_path / "heartbeat.json"
    payload = json.dumps({"pid": 1, "ts": time.time(), "version": 1})
    hb.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    with mock.patch.object(jan, "HEARTBEAT_PATH", hb):
        assert jan.janitor_alive() is True
