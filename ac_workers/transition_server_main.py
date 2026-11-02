"""Standalone transition server process (single instance per session)."""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from common.transition_server import TransitionServer, transition_server_port


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    port = transition_server_port()
    if args.dry_run:
        print("transition-server dry-run port={0}".format(port))
        return 0
    server = TransitionServer()
    server.start()
    print("transition server listening on 127.0.0.1:{0} pid={1}".format(port, __import__("os").getpid()))

    def _stop(_sig=None, _frame=None):
        server.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    try:
        while True:
            time.sleep(1.0)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
