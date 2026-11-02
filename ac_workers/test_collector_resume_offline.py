"""Offline tests for collector resume sentinel."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_resume_sentinel_round_trip(tmp_path):
    import ac_workers.collector_resume as cr

    with mock.patch.object(cr, "STATUS_DIR", tmp_path):
        assert cr.collector_resume_requested(2) is False
        assert cr.request_collector_resume(2) is True
        assert cr.collector_resume_requested(2) is True
        cr.clear_collector_resume(2)
        assert cr.collector_resume_requested(2) is False
