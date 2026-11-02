"""Ultra-minimal diag ping debug tool.

Usage:
    python -m ac_workers.collector_diag_ping --worker 0
    python -m ac_workers.collector_diag_ping --port 24100
    python -m ac_workers.collector_diag_ping --selftest
    python -m ac_workers.collector_diag_ping --all

Shows step-by-step what happens at the TCP/protocol level.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIAG_BASE_PORT = 24100
HOST = "127.0.0.1"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def step_by_step_ping(port: int, cmd: str = "ping", timeout: float = 3.0) -> dict:
    """Raw TCP ping with full step-by-step output. Returns result dict."""
    result = {
        "port": port,
        "cmd": cmd,
        "socket_created": False,
        "connect_ok": False,
        "send_ok": False,
        "recv_bytes": 0,
        "raw_response": b"",
        "json_parsed": False,
        "json_response": None,
        "error": "",
        "traceback": "",
        "elapsed_ms": 0.0,
    }
    t0 = time.time()
    sock = None
    try:
        print("  [{0}] step 1: creating socket AF_INET SOCK_STREAM".format(_ts()))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result["socket_created"] = True
        print("  [{0}] step 1: OK socket created fd={1}".format(_ts(), sock.fileno()))

        print("  [{0}] step 2: settimeout({1}s)".format(_ts(), timeout))
        sock.settimeout(timeout)
        print("  [{0}] step 2: OK".format(_ts()))

        print("  [{0}] step 3: connect({1}, {2})".format(_ts(), HOST, port))
        sock.connect((HOST, port))
        result["connect_ok"] = True
        print("  [{0}] step 3: OK connected".format(_ts()))

        # Build request exactly as CollectorDiagClient does (incl. request_id)
        worker_id = port - DIAG_BASE_PORT
        request_id = uuid.uuid4().hex[:8]
        result["request_id"] = request_id
        msg = {"cmd": cmd, "worker_id": worker_id, "request_id": request_id}
        data = (json.dumps(msg) + "\n").encode("utf-8")
        print("  [{0}] step 4: sending {1} bytes rid={2}: {3}".format(
            _ts(), len(data), request_id, data[:200]
        ))
        sock.sendall(data)
        result["send_ok"] = True
        print("  [{0}] step 4: OK sendall complete".format(_ts()))

        print("  [{0}] step 5: recv (waiting for response...)".format(_ts()))
        buf = b""
        while b"\n" not in buf:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                print("  [{0}] step 5: TIMEOUT during recv (no data after {1}s)".format(_ts(), timeout))
                result["error"] = "recv timeout"
                break
            if not chunk:
                print("  [{0}] step 5: connection closed by server (0 bytes)".format(_ts()))
                result["error"] = "connection closed by server before response"
                break
            buf += chunk
            print("  [{0}] step 5: received {1} bytes (total {2})".format(_ts(), len(chunk), len(buf)))

        result["recv_bytes"] = len(buf)
        result["raw_response"] = buf

        if buf.strip():
            line = buf.split(b"\n", 1)[0]
            print("  [{0}] step 6: raw line: {1}".format(_ts(), line[:500]))
            try:
                parsed = json.loads(line.decode("utf-8"))
                result["json_parsed"] = True
                result["json_response"] = parsed
                print("  [{0}] step 6: JSON parsed OK".format(_ts()))
                print("  [{0}] step 6: ok={1} cmd={2} worker_id={3} boot_id={4}".format(
                    _ts(),
                    parsed.get("ok"),
                    parsed.get("cmd"),
                    parsed.get("worker_id"),
                    str(parsed.get("boot_id", ""))[:12],
                ))
                if parsed.get("error"):
                    print("  [{0}] step 6: error={1}".format(_ts(), parsed.get("error")))
            except (ValueError, json.JSONDecodeError) as exc:
                result["error"] = "JSON parse failed: {0}".format(exc)
                print("  [{0}] step 6: FAILED to parse JSON: {1}".format(_ts(), exc))
        elif not result["error"]:
            result["error"] = "empty response (0 useful bytes)"
            print("  [{0}] step 6: EMPTY response".format(_ts()))

    except ConnectionRefusedError as exc:
        result["error"] = "connection refused"
        result["traceback"] = traceback.format_exc()
        print("  [{0}] FAILED: connection refused on port {1}".format(_ts(), port))
    except socket.timeout as exc:
        result["error"] = "connect timeout"
        result["traceback"] = traceback.format_exc()
        print("  [{0}] FAILED: connect timeout on port {1}".format(_ts(), port))
    except OSError as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
        print("  [{0}] FAILED: OSError: {1}".format(_ts(), exc))
        print("  [{0}]   errno={1} winerror={2}".format(
            _ts(), exc.errno, getattr(exc, "winerror", None)
        ))
        print("  [{0}]   full traceback:".format(_ts()))
        traceback.print_exc()
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
        print("  [{0}] FAILED: {1}: {2}".format(_ts(), type(exc).__name__, exc))
        traceback.print_exc()
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        result["elapsed_ms"] = (time.time() - t0) * 1000.0

    return result


def selftest() -> bool:
    """Start a minimal diag server on a temp port and ping it.

    Proves the protocol works without any collector/worker/runtime.
    """
    import random

    port = random.randint(25500, 25999)
    print("=== SELFTEST: starting mini diag server on port {0} ===".format(port))

    server_ready = threading.Event()
    server_error = [None]

    def _mini_server():
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((HOST, port))
            srv.listen(4)
            srv.settimeout(5.0)
            server_ready.set()
            conn, _addr = srv.accept()
            conn.settimeout(2.0)
            # Read request
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            # Parse
            line = buf.split(b"\n", 1)[0]
            req = json.loads(line.decode("utf-8"))
            # Always respond with ping
            resp = {
                "ok": True,
                "cmd": req.get("cmd", "ping"),
                "worker_id": req.get("worker_id", -1),
                "collector_pid": os.getpid(),
                "boot_id": "selftest_boot",
                "diag_port": port,
                "ts": time.time(),
            }
            data = (json.dumps(resp) + "\n").encode("utf-8")
            conn.sendall(data)
            conn.close()
            srv.close()
        except Exception as exc:
            server_error[0] = exc
            server_ready.set()

    t = threading.Thread(target=_mini_server, daemon=True)
    t.start()
    if not server_ready.wait(3.0):
        print("SELFTEST FAILED: server did not start")
        return False
    if server_error[0]:
        print("SELFTEST FAILED: server error: {0}".format(server_error[0]))
        return False

    print("=== SELFTEST: server ready, sending ping to port {0} ===".format(port))
    result = step_by_step_ping(port, cmd="ping", timeout=3.0)
    t.join(timeout=3.0)

    if result["json_parsed"] and result["json_response"] and result["json_response"].get("ok"):
        print("\n=== SELFTEST PASSED: protocol works (tcp+send+recv+json all OK) ===")
        return True
    else:
        print("\n=== SELFTEST FAILED: {0} ===".format(result["error"] or "no valid JSON"))
        return False


def ping_all_workers(worker_ids: list[int] | None = None, cmd: str = "ping") -> None:
    """Ping all workers (or specified ones) and print summary."""
    ids = worker_ids if worker_ids is not None else list(range(6))
    results = []
    for wid in ids:
        port = DIAG_BASE_PORT + wid
        print("\n--- Worker {0} port {1} cmd={2} ---".format(wid, port, cmd))
        r = step_by_step_ping(port, cmd=cmd)
        results.append(r)
        if r["json_parsed"] and r.get("json_response", {}).get("ok"):
            print("  RESULT: OK (boot_id={0})".format(
                str(r["json_response"].get("boot_id", ""))[:12]
            ))
        else:
            print("  RESULT: FAILED ({0})".format(r["error"] or "no ok=true"))

    print("\n" + "=" * 60)
    print("SUMMARY:")
    print("  {0:<6} {1:<6} {2:<6} {3:<6} {4:<6} {5}".format(
        "W", "tcp", "send", "resp", "json", "error/boot_id"
    ))
    for i, (wid, r) in enumerate(zip(ids, results)):
        ok = r["json_parsed"] and r.get("json_response", {}).get("ok")
        boot = str(r.get("json_response", {}).get("boot_id", ""))[:12] if ok else ""
        err = r["error"][:40] if r["error"] else ""
        print("  {0:<6} {1:<6} {2:<6} {3:<6} {4:<6} {5}".format(
            wid,
            "yes" if r["connect_ok"] else "NO",
            "yes" if r["send_ok"] else "NO",
            "yes" if r["recv_bytes"] > 0 else "NO",
            "yes" if r["json_parsed"] else "NO",
            boot if ok else err,
        ))
    passed = sum(1 for r in results if r["json_parsed"] and r.get("json_response", {}).get("ok"))
    print("\ndiag-ping: {0}/{1} workers responded OK to '{2}'".format(passed, len(ids), cmd))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Diag ping debug tool")
    parser.add_argument("--worker", "-w", type=int, default=None, help="Worker ID (0-5)")
    parser.add_argument("--port", "-p", type=int, default=None, help="Direct port number")
    parser.add_argument("--cmd", type=str, default="ping", help="Command to send (default: ping)")
    parser.add_argument("--all", action="store_true", help="Ping all 6 workers")
    parser.add_argument("--selftest", action="store_true", help="Run protocol self-test (local server)")
    parser.add_argument("--timeout", type=float, default=3.0, help="Socket timeout (seconds)")
    parser.add_argument("--workers", type=str, default=None, help="Worker range e.g. 0-5 or 0,2,4")
    args = parser.parse_args()

    print("collector_diag_ping.py — raw TCP diagnostic probe")
    print("pid={0} python={1}".format(os.getpid(), sys.version.split()[0]))
    print()

    if args.selftest:
        ok = selftest()
        if not ok:
            return 1
        print()
        # Also try real workers after selftest
        if args.all or args.workers:
            pass  # fall through
        else:
            return 0

    if args.port is not None:
        print("--- Direct port {0} cmd={1} ---".format(args.port, args.cmd))
        r = step_by_step_ping(args.port, cmd=args.cmd, timeout=args.timeout)
        return 0 if r["json_parsed"] else 1

    if args.worker is not None:
        port = DIAG_BASE_PORT + args.worker
        print("--- Worker {0} port {1} cmd={2} ---".format(args.worker, port, args.cmd))
        r = step_by_step_ping(port, cmd=args.cmd, timeout=args.timeout)
        return 0 if r["json_parsed"] else 1

    # Parse --workers or --all
    worker_ids = None
    if args.workers:
        if "-" in args.workers:
            parts = args.workers.split("-")
            worker_ids = list(range(int(parts[0]), int(parts[1]) + 1))
        else:
            worker_ids = [int(x) for x in args.workers.split(",")]
    elif args.all:
        worker_ids = list(range(6))
    else:
        # Default: selftest if nothing specified
        ok = selftest()
        return 0 if ok else 1

    ping_all_workers(worker_ids, cmd=args.cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
