"""Verify all vJoy devices can be acquired (used by Reset-vJoy-Driver.ps1)."""
from __future__ import annotations

import os
import sys
from importlib import reload
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "assetto_corsa_gym" / "AssettoCorsaPlugin" / "plugins" / "sensors_par"))

from ac_workers.vjoy_session import get_vjd_status, try_acquire_vjoy, vjd_status_label


def main() -> int:
    count = int(os.environ.get("VJOY_VERIFY_COUNT", "6"))
    bad = 0
    for device_id in range(1, count + 1):
        os.environ["ACGYM_VJOY_DEVICE"] = str(device_id)
        import vjoy as vjmod  # noqa: WPS433

        reload(vjmod)
        vj = vjmod.vJoy(reference=device_id)
        acquired, err = try_acquire_vjoy(vj, device_id)
        status = vjd_status_label(get_vjd_status(device_id))
        print(
            "device {0} {1} acquire_ok={2} {3}".format(
                device_id, status, acquired, err or ""
            )
        )
        if not acquired:
            bad += 1
        try:
            vj.close()
        except Exception:
            pass
        try:
            from ac_workers.vjoy_session import relinquish_vjd

            relinquish_vjd(device_id)
        except Exception:
            pass
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
