#!/usr/bin/env python3
"""Calibre steer_map.csv pour lotus_exos_125 via vJoy + telemetrie sensors_par."""

from __future__ import annotations

import csv
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

VJOY_DLL = r"C:\Program Files\vJoy\x64\vJoyInterface.dll"
AI_ACS = r"C:\Leo projet\Assetto Corsa ai\AssettoCorsa-AI\assettocorsa\acs.exe"
TELEM_HOST = "127.0.0.1"
TELEM_PORT = 2345
TELEM_WAIT_SEC = 15.0
TELEM_RETRY_SEC = 0.5
SCALE = 16384
DEVICE = 1
SPEED_LIMIT_KMH = 1.0
ACC_MAX = 0.02
BRAKE_HOLD = 0.12
STEER_STEP = 0.02
STEER_HOLD_SEC = 0.18
PLATEAU_DELTA_DEG = 0.4
PLATEAU_SAMPLES = 4

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_CSV = (
    REPO_ROOT
    / "assetto_corsa_gym"
    / "AssettoCorsaConfigs"
    / "cars"
    / "lotus_exos_125"
    / "steer_map.csv"
)
PLUGIN_DIR = REPO_ROOT / "assetto_corsa_gym" / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
sys.path.insert(0, str(PLUGIN_DIR))

from vjoy import vJoy  # noqa: E402


class TelemetryClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._latest)

    @staticmethod
    def _parse_json(msg: str) -> dict | None:
        if not msg or msg in ("identified", "disconnect"):
            return None
        if not (msg.startswith("{") and msg.endswith("}")):
            return None
        try:
            return json.loads(msg)
        except json.JSONDecodeError:
            return None

    def connect(self) -> None:
        deadline = time.time() + TELEM_WAIT_SEC
        while time.time() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(TELEM_RETRY_SEC)
            try:
                sock.sendto(b"connect", (TELEM_HOST, TELEM_PORT))
            except OSError:
                sock.close()
                time.sleep(TELEM_RETRY_SEC)
                continue
            end = min(time.time() + TELEM_RETRY_SEC, deadline)
            while time.time() < end:
                try:
                    data, _ = sock.recvfrom(65536)
                except (socket.timeout, ConnectionResetError, OSError):
                    break
                msg = data.decode("utf-8", errors="replace").strip()
                if msg == "identified":
                    continue
                payload = self._parse_json(msg)
                if payload is not None:
                    with self._lock:
                        self._latest = payload
                    self._sock = sock
                    self._thread = threading.Thread(target=self._loop, daemon=True)
                    self._thread.start()
                    return
            sock.close()
            time.sleep(TELEM_RETRY_SEC)
        raise RuntimeError("Telemetrie indisponible sur 127.0.0.1:2345")

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65536)
            except (socket.timeout, ConnectionResetError, OSError):
                continue
            payload = self._parse_json(data.decode("utf-8", errors="replace").strip())
            if payload is not None:
                with self._lock:
                    self._latest = payload

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.sendto(b"disconnect", (TELEM_HOST, TELEM_PORT))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass


def normalize_path(path: str) -> str:
    return str(Path(path).resolve()).lower()


def verify_ai_acs_running() -> None:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='acs.exe'\" -ErrorAction SilentlyContinue"
        " | Select-Object -ExpandProperty ExecutablePath"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not paths:
        raise RuntimeError("acs.exe introuvable - session Hotlap AI requise.")
    if not any(normalize_path(p) == normalize_path(AI_ACS) for p in paths):
        raise RuntimeError(f"acs.exe actif ne provient pas de la copie AI: {paths}")


def check_stationary(telem: dict, label: str) -> None:
    speed_kmh = float(telem.get("speed", 0.0)) * 3.6
    if speed_kmh > SPEED_LIMIT_KMH:
        raise RuntimeError(f"ARRET [{label}] vitesse {speed_kmh:.2f} km/h > {SPEED_LIMIT_KMH:.1f}")
    acc = float(telem.get("accStatus", 0.0))
    if acc > ACC_MAX:
        raise RuntimeError(f"ARRET [{label}] acc={acc:.3f} (accelerateur doit rester a zero)")


def apply_vjoy(vj: vJoy, *, steer: float = 1.0, acc: float = 0.0, brake: float = 0.0) -> None:
    pos = vj.generateJoystickPosition(
        wAxisX=int(steer * SCALE),
        wAxisY=int(acc * 2 * SCALE),
        wAxisZ=int(brake * 2 * SCALE),
        lButtons=0,
    )
    if not vj.update(pos):
        raise RuntimeError("UpdateVJD a echoue")


def vjoy_to_action_steer(vjoy_steer: float) -> float:
    return vjoy_steer - 1.0


def wait_enter(step: str) -> None:
    input(f"\n[{step}] Entree pour continuer...")


def sweep_steer(
    vj: vJoy,
    telem: TelemetryClient,
    start: float,
    stop: float,
    step: float,
) -> tuple[float, float, float]:
    """Retourne (vjoy au plateau, steerAngle plateau, action_steerAngle)."""
    direction = 1.0 if stop > start else -1.0
    pos = start
    best_vjoy = start
    best_angle = float(telem.snapshot().get("steerAngle", 0.0))
    prev_angle = best_angle
    stable = 0

    while (direction > 0 and pos <= stop + 1e-9) or (direction < 0 and pos >= stop - 1e-9):
        apply_vjoy(vj, steer=pos, acc=0.0, brake=BRAKE_HOLD)
        time.sleep(STEER_HOLD_SEC)
        snap = telem.snapshot()
        check_stationary(snap, "sweep")
        angle = float(snap.get("steerAngle", 0.0))
        print(f"  vJoy steer={pos:.3f}  steerAngle={angle:.3f}")

        if abs(angle - prev_angle) <= PLATEAU_DELTA_DEG:
            stable += 1
        else:
            stable = 0
            best_vjoy = pos
            best_angle = angle

        prev_angle = angle
        if stable >= PLATEAU_SAMPLES:
            break
        pos += direction * step

    action = vjoy_to_action_steer(best_vjoy)
    return best_vjoy, best_angle, action


def write_steer_map(left_action: float, left_deg: float, right_action: float, right_deg: float) -> None:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ["action_steerAngle", "steerAngle"],
        [f"{left_action:.10g}", f"{left_deg:.10g}"],
        [f"{right_action:.10g}", f"{right_deg:.10g}"],
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="ascii") as fh:
        writer = csv.writer(fh)
        writer.writerows(rows)

    lines = OUTPUT_CSV.read_text(encoding="ascii").strip().splitlines()
    if len(lines) != 3:
        raise RuntimeError(f"steer_map.csv invalide: {len(lines)} lignes (attendu: 3)")

    print(f"\nEcrit: {OUTPUT_CSV}")
    print(f"  gauche : action={left_action:.6f}  steerAngle={left_deg:.3f}")
    print(f"  droite : action={right_action:.6f}  steerAngle={right_deg:.3f}")


def main() -> int:
    if not Path(VJOY_DLL).is_file():
        print(f"ERREUR: DLL vJoy introuvable: {VJOY_DLL}")
        return 1

    telem = TelemetryClient()
    vj: vJoy | None = None
    try:
        verify_ai_acs_running()
        telem.connect()
        print("Telemetrie OK.")

        vj = vJoy(reference=DEVICE)
        if not vj.open():
            raise RuntimeError(f"impossible d'acquerir vJoy Device {DEVICE}")

        apply_vjoy(vj, steer=1.0, acc=0.0, brake=BRAKE_HOLD)
        time.sleep(0.5)
        snap = telem.snapshot()
        check_stationary(snap, "initial")
        print(
            f"Neutre frein={BRAKE_HOLD:.2f} speed={float(snap.get('speed', 0))*3.6:.2f} km/h "
            f"steerAngle={float(snap.get('steerAngle', 0.0)):.3f}"
        )

        wait_enter("1/2 balayage GAUCHE (lent)")
        left_vjoy, left_deg, left_action = sweep_steer(vj, telem, 1.0, 0.0, STEER_STEP)
        apply_vjoy(vj, steer=1.0, acc=0.0, brake=BRAKE_HOLD)
        time.sleep(0.5)

        wait_enter("2/2 balayage DROITE (lent)")
        right_vjoy, right_deg, right_action = sweep_steer(vj, telem, 1.0, 2.0, STEER_STEP)

        if left_deg >= -1.0:
            raise RuntimeError(f"limite gauche non detectee (steerAngle={left_deg:.3f})")
        if right_deg <= 1.0:
            raise RuntimeError(f"limite droite non detectee (steerAngle={right_deg:.3f})")

        write_steer_map(left_action, left_deg, right_action, right_deg)
        return 0

    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130
    except RuntimeError as exc:
        print(f"ERREUR: {exc}")
        return 1
    finally:
        if vj is not None:
            try:
                apply_vjoy(vj, steer=1.0, acc=0.0, brake=BRAKE_HOLD)
                time.sleep(1.0)
                apply_vjoy(vj, steer=1.0, acc=0.0, brake=0.0)
            except Exception:
                pass
            try:
                vj.close()
            except Exception:
                pass
        telem.stop()
        print("Neutre restaure, vJoy libere.")


if __name__ == "__main__":
    raise SystemExit(main())
