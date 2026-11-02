#!/usr/bin/env python3
"""Lecture seule de la telemetrie ego (port 2345). Aucune commande de conduite."""

import json
import socket
import sys
import time

HOST = "127.0.0.1"
PORT = 2345
PRINT_INTERVAL = 0.5


def main() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    server = (HOST, PORT)

    try:
        sock.sendto(b"connect", server)
    except OSError as exc:
        print(f"ERREUR : impossible d'envoyer connect vers {HOST}:{PORT} — {exc}", file=sys.stderr)
        return 1

    identified = False
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            continue
        msg = data.decode("utf-8", errors="replace").strip()
        if msg == "identified":
            identified = True
            print(f"Connecte a {addr[0]}:{addr[1]} — en attente de telemetrie (Ctrl+C pour arreter)...")
            break
        if msg and msg[0] == "{":
            identified = True
            print(f"Connecte (JSON direct) — en attente de telemetrie...")
            break

    if not identified:
        print(f"ERREUR : pas de reponse 'identified' sur {HOST}:{PORT}. Le plug-in sensors_par est-il actif ?", file=sys.stderr)
        sock.close()
        return 1

    last_print = 0.0
    sock.settimeout(1.0)
    try:
        while True:
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            msg = data.decode("utf-8", errors="replace").strip()
            if not msg or msg in ("identified", "disconnect"):
                continue
            try:
                payload = json.loads(msg)
            except json.JSONDecodeError:
                continue

            now = time.time()
            if now - last_print < PRINT_INTERVAL:
                continue
            last_print = now

            speed_ms = payload.get("speed", 0.0)
            speed_kmh = float(speed_ms) * 3.6
            print(
                "steps={steps} speed={speed:.2f}m/s ({kmh:.1f}km/h) "
                "pos=({x:.2f},{y:.2f},{z:.2f}) RPM={rpm} gear={gear} "
                "steer={steer:.3f} spline={spline:.4f}".format(
                    steps=payload.get("steps", "?"),
                    speed=float(speed_ms),
                    kmh=speed_kmh,
                    x=float(payload.get("world_position_x", 0.0)),
                    y=float(payload.get("world_position_y", 0.0)),
                    z=float(payload.get("world_position_z", 0.0)),
                    rpm=payload.get("RPM", "?"),
                    gear=payload.get("actualGear", "?"),
                    steer=float(payload.get("steerAngle", 0.0)),
                    spline=float(payload.get("NormalizedSplinePosition", 0.0)),
                )
            )
    except KeyboardInterrupt:
        print("\nArret demande — envoi disconnect...")
    finally:
        try:
            sock.sendto(b"disconnect", server)
        except OSError:
            pass
        sock.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
