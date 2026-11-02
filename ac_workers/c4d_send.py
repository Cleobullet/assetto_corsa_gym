#!/usr/bin/env python
"""Send a single command to the C4D server and print the reply (no 'quit').

Usage: python -m ac_workers.c4d_send <line...>
"""
import sys

from ac_workers import c4d_ipc

HOST = "127.0.0.1"
PORT = 24700
TOKEN = "5fc7b07f64f444e2b5ce23741f24f0d1"
REPO = r"C:\Leo projet\Assetto Corsa ai"


def main():
    args = sys.argv[1:]
    token = TOKEN
    if args and args[0].startswith("--token="):
        token = args.pop(0).split("=", 1)[1]
    line = " ".join(args) if args else "status"
    sock, buf = c4d_ipc.connect_client(HOST, PORT, "console", token=token, repo_root=REPO)
    try:
        c4d_ipc._send_msg(sock, {"t": "cmd", "line": line})
        reply = c4d_ipc._recv_line(sock, buf)
        if reply:
            print(reply.get("text") or "")
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
