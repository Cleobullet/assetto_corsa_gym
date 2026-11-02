"""Capture sys.argv for redirected-process offline tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if "--argv-out" in sys.argv:
        out_idx = sys.argv.index("--argv-out")
        out_path = Path(sys.argv[out_idx + 1])
        if str(out_path) != "IGNORED":
            out_path.write_text(json.dumps(list(sys.argv)), encoding="utf-8")
    print(json.dumps(list(sys.argv)))
    if "--exit-code" in sys.argv:
        return int(sys.argv[sys.argv.index("--exit-code") + 1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
