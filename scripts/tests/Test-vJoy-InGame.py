#!/usr/bin/env python3
"""Test prudents vJoy in-game via telemetrie (copie AI uniquement)."""

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
SPEED_LIMIT_KMH = 15.0
STEER_OFFSET = 0.03
STEER_CENTER_ABS_MAX = 1.5
STEER_TURN_ABS_MIN = 5.0
BRAKE_GAMMA = 2.4
BRAKE_REST_MARGIN = 0.012
BRAKE_EXPECT_TOLERANCE = 0.015

PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent
    / "assetto_corsa_gym"
    / "AssettoCorsaPlugin"
    / "plugins"
    / "sensors_par"
)
sys.path.insert(0, str(PLUGIN_DIR))

from vjoy import vJoy  # noqa: E402


class TelemetryClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict = {}
        self._stop = threading.Event()
        self._emergency = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._ready = False

    @property
    def emergency(self) -> bool:
        return self._emergency.is_set()

    @property
    def ready(self) -> bool:
        return self._ready

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._latest)

    @staticmethod
    def _new_socket() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(TELEM_RETRY_SEC)
        return sock

    @staticmethod
    def _parse_json_packet(msg: str) -> dict | None:
        if not msg or msg in ("identified", "disconnect"):
            return None
        if not (msg.startswith("{") and msg.endswith("}")):
            return None
        try:
            return json.loads(msg)
        except json.JSONDecodeError:
            return None

    def connect(self) -> None:
        """Attend identified + 1 paquet JSON. Ne lance pas si indisponible."""
        deadline = time.time() + TELEM_WAIT_SEC
        last_detail = "aucune reponse"
        identified = False

        while time.time() < deadline:
            sock = self._new_socket()
            try:
                sock.sendto(b"connect", (TELEM_HOST, TELEM_PORT))
            except OSError as exc:
                last_detail = f"envoi connect impossible : {exc}"
                sock.close()
                time.sleep(TELEM_RETRY_SEC)
                continue

            attempt_end = min(time.time() + TELEM_RETRY_SEC, deadline)
            while time.time() < attempt_end:
                try:
                    data, _ = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    last_detail = (
                        f"port {TELEM_PORT} refuse la connexion "
                        "(sensors_par inactif ou session sans plug-in ?)"
                    )
                    break
                except OSError as exc:
                    last_detail = f"erreur socket : {exc}"
                    break

                msg = data.decode("utf-8", errors="replace").strip()
                if msg == "identified":
                    identified = True
                    last_detail = "identified recu, attente telemetrie JSON..."
                    continue

                payload = self._parse_json_packet(msg)
                if payload is not None:
                    if not identified:
                        identified = True
                    with self._lock:
                        self._latest = payload
                    self._sock = sock
                    self._ready = True
                    self._thread = threading.Thread(target=self._loop, daemon=True)
                    self._thread.start()
                    print(
                        f"Telemetrie OK : identified + JSON "
                        f"(steps={payload.get('steps', '?')})"
                    )
                    return

            sock.close()
            if identified and not self._ready:
                # identified recu : laisser plus de temps pour le 1er JSON
                extra_end = min(time.time() + 3.0, deadline)
                sock = self._new_socket()
                try:
                    sock.sendto(b"connect", (TELEM_HOST, TELEM_PORT))
                    while time.time() < extra_end:
                        try:
                            data, _ = sock.recvfrom(65536)
                        except socket.timeout:
                            continue
                        except ConnectionResetError:
                            last_detail = f"connexion perdue apres identified"
                            break
                        except OSError as exc:
                            last_detail = f"erreur socket : {exc}"
                            break
                        msg = data.decode("utf-8", errors="replace").strip()
                        if msg == "identified":
                            continue
                        payload = self._parse_json_packet(msg)
                        if payload is not None:
                            with self._lock:
                                self._latest = payload
                            self._sock = sock
                            self._ready = True
                            self._thread = threading.Thread(target=self._loop, daemon=True)
                            self._thread.start()
                            print(
                                f"Telemetrie OK : identified + JSON "
                                f"(steps={payload.get('steps', '?')})"
                            )
                            return
                finally:
                    if not self._ready:
                        sock.close()

            time.sleep(TELEM_RETRY_SEC)

        raise RuntimeError(
            f"Telemetrie indisponible sur {TELEM_HOST}:{TELEM_PORT} apres "
            f"{TELEM_WAIT_SEC:.0f}s.\n"
            f"Dernier etat : {last_detail}\n"
            "Verifiez : session AI active, plug-in sensors_par active dans CM, py_log.txt."
        )

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            except OSError:
                break

            msg = data.decode("utf-8", errors="replace").strip()
            payload = self._parse_json_packet(msg)
            if payload is None:
                continue

            with self._lock:
                self._latest = payload
            speed_kmh = float(payload.get("speed", 0.0)) * 3.6
            if speed_kmh > SPEED_LIMIT_KMH:
                self._emergency.set()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.sendto(b"disconnect", (TELEM_HOST, TELEM_PORT))
            except (ConnectionResetError, OSError):
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=2.0)


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
        raise RuntimeError("acs.exe introuvable — lancez une session Hotlap depuis Content Manager AI.")
    ai_norm = normalize_path(AI_ACS)
    if not any(normalize_path(p) == ai_norm for p in paths):
        raise RuntimeError(
            "acs.exe actif ne provient pas de la copie AI.\n"
            f"Attendu : {AI_ACS}\n"
            f"Trouve  : {paths}"
        )
    print(f"OK acs.exe AI detecte : {AI_ACS}")


def apply_vjoy(vj: vJoy, *, steer: float = 1.0, acc: float = 0.0, brake: float = 0.0, buttons: int = 0) -> None:
    pos = vj.generateJoystickPosition(
        wAxisX=int(steer * SCALE),
        wAxisY=int(acc * 2 * SCALE),
        wAxisZ=int(brake * 2 * SCALE),
        lButtons=buttons,
    )
    if not vj.update(pos):
        raise RuntimeError("UpdateVJD a echoue")


def neutre_vjoy(vj: vJoy) -> None:
    apply_vjoy(vj, steer=1.0, acc=0.0, brake=0.0, buttons=0)


def afficher_telem(label: str, telem: dict) -> None:
    speed_kmh = float(telem.get("speed", 0.0)) * 3.6
    print(
        f"  [{label}] speed={speed_kmh:.1f} km/h RPM={telem.get('RPM', '?')} "
        f"steer={float(telem.get('steerAngle', 0.0)):.3f} "
        f"acc={float(telem.get('accStatus', 0.0)):.3f} "
        f"brake={float(telem.get('brakeStatus', 0.0)):.3f}"
    )


def attendre_entree(etape: str) -> None:
    input(f"\n[{etape}] Entree pour lancer le test...")


def hold(
    vj: vJoy,
    telem: TelemetryClient,
    duration: float,
    label: str,
    *,
    steer: float = 1.0,
    acc: float = 0.0,
    brake: float = 0.0,
) -> list[dict]:
    samples: list[dict] = []
    end = time.time() + duration
    while time.time() < end:
        if telem.emergency:
            raise RuntimeError(f"ARRET SECURITE : vitesse > {SPEED_LIMIT_KMH:.0f} km/h")
        apply_vjoy(vj, steer=steer, acc=acc, brake=brake)
        snap = telem.snapshot()
        samples.append(snap)
        afficher_telem(label, snap)
        time.sleep(0.15)
    return samples


def observed_steer_left(samples: list[dict]) -> bool:
    return any(float(s.get("steerAngle", 0.0)) < -STEER_TURN_ABS_MIN for s in samples)


def observed_steer_right(samples: list[dict]) -> bool:
    return any(float(s.get("steerAngle", 0.0)) > STEER_TURN_ABS_MIN for s in samples)


def observed_steer_center(samples: list[dict]) -> bool:
    return all(abs(float(s.get("steerAngle", 0.0))) <= STEER_CENTER_ABS_MAX for s in samples)


def brake_expected(command: float) -> float:
    return command ** BRAKE_GAMMA


def observed_brake_press(samples: list[dict], command: float, baseline_brake: float = 0.0) -> bool:
    """Valide le frein via brakeStatus transforme (GAMMA preset), pas la commande brute."""
    if command <= 0:
        return observed_brake_rest(samples, baseline_brake)
    rest = baseline_brake
    expected = brake_expected(command)
    tolerance = max(BRAKE_EXPECT_TOLERANCE, expected * 0.25)
    for sample in samples:
        brake = float(sample.get("brakeStatus", 0.0))
        if abs(brake - expected) <= tolerance:
            return True
        if brake >= rest + BRAKE_REST_MARGIN:
            return True
    return False


def observed_brake_rest(samples: list[dict], baseline_brake: float = 0.0) -> bool:
    limit = baseline_brake + BRAKE_REST_MARGIN
    return all(float(s.get("brakeStatus", 0.0)) <= limit for s in samples)


def observed_acc(samples: list[dict], minimum: float = 0.03) -> bool:
    return any(float(s.get("accStatus", 0.0)) >= minimum for s in samples)


def observed_acc_zero(samples: list[dict], maximum: float = 0.05) -> bool:
    return all(float(s.get("accStatus", 0.0)) <= maximum for s in samples)


def arret_securite(vj: vJoy | None, reason: str) -> None:
    print(f"\n!!! ARRET SECURITE : {reason}")
    if vj is None:
        return
    apply_vjoy(vj, steer=1.0, acc=0.0, brake=0.5)
    time.sleep(2.0)
    apply_vjoy(vj, steer=1.0, acc=0.0, brake=0.0)


def main() -> int:
    if not Path(VJOY_DLL).is_file():
        print(f"ERREUR : DLL vJoy introuvable : {VJOY_DLL}")
        return 1

    try:
        verify_ai_acs_running()
    except RuntimeError as exc:
        print(f"ERREUR : {exc}")
        return 1

    telem = TelemetryClient()
    vj: vJoy | None = None
    results: list[tuple[str, bool]] = []
    exit_code = 1

    try:
        telem.connect()

        vj = vJoy(reference=DEVICE)
        if not vj.open():
            raise RuntimeError(f"impossible d'acquerir vJoy Device {DEVICE}")

        neutre_vjoy(vj)
        print("vJoy acquis. Etat neutre applique.")
        time.sleep(0.5)
        baseline = telem.snapshot()
        if not baseline:
            raise RuntimeError("telemetrie vide apres connexion")
        afficher_telem("neutre", baseline)
        baseline_brake = float(baseline.get("brakeStatus", 0.0))

        tests = [
            ("1/8 direction gauche ~3 %", lambda: hold(
                vj, telem, 1.0, "gauche", steer=1.0 - STEER_OFFSET)),
            ("2/8 retour centre", lambda: hold(vj, telem, 1.0, "centre")),
            ("3/8 direction droite ~3 %", lambda: hold(
                vj, telem, 1.0, "droite", steer=1.0 + STEER_OFFSET)),
            ("4/8 retour centre", lambda: hold(vj, telem, 1.0, "centre")),
            ("5/8 frein 25 %", lambda: hold(vj, telem, 1.0, "frein25", brake=0.25)),
            ("6/8 accelerateur 6 % (max 0.8 s)", lambda: hold(
                vj, telem, 0.8, "acc6", acc=0.06)),
            ("7/8 frein 35 % (acc zero)", lambda: hold(
                vj, telem, 2.0, "frein35", acc=0.0, brake=0.35)),
            ("8/8 neutre complet", lambda: hold(vj, telem, 1.0, "neutre")),
        ]

        observers = [
            lambda s: observed_steer_left(s),
            lambda s: observed_steer_center(s),
            lambda s: observed_steer_right(s),
            lambda s: observed_steer_center(s),
            lambda s: observed_brake_press(s, 0.25, baseline_brake),
            lambda s: observed_acc(s, 0.03),
            lambda s: observed_brake_press(s, 0.35, baseline_brake) and observed_acc_zero(s, 0.05),
            lambda s: (
                observed_steer_center(s)
                and observed_acc_zero(s, 0.05)
                and observed_brake_rest(s, baseline_brake)
            ),
        ]

        for (label, run_test), observe in zip(tests, observers):
            attendre_entree(label)
            if telem.emergency:
                raise RuntimeError(f"vitesse > {SPEED_LIMIT_KMH:.0f} km/h avant le test")
            samples = run_test()
            ok = observe(samples)
            results.append((label, ok))
            print(f"  -> {'OBSERVE' if ok else 'NON OBSERVE'} dans la telemetrie")

        print("\n=== Resume ===")
        for label, ok in results:
            print(f"  {'OK' if ok else 'ECHEC'} : {label}")
        exit_code = 0 if all(ok for _, ok in results) else 2

    except KeyboardInterrupt:
        arret_securite(vj, "Ctrl+C")
        exit_code = 130
    except RuntimeError as exc:
        msg = str(exc)
        if "ARRET SECURITE" in msg or "vitesse" in msg.lower():
            arret_securite(vj, msg)
        else:
            print(f"ERREUR : {msg}")
        exit_code = 1
    except (ConnectionResetError, OSError, socket.timeout) as exc:
        print(
            f"ERREUR : communication telemetrie interrompue ({exc}).\n"
            "Verifiez que sensors_par est actif et que la session AI est en piste."
        )
        exit_code = 1
    finally:
        if vj is not None:
            try:
                neutre_vjoy(vj)
                apply_vjoy(vj, steer=1.0, acc=0.0, brake=0.5)
                time.sleep(2.0)
                apply_vjoy(vj, steer=1.0, acc=0.0, brake=0.0)
            except Exception:
                pass
            try:
                vj.close()
            except Exception:
                pass
            print("vJoy libere.")
        telem.stop()
        print("Telemetrie deconnectee.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
