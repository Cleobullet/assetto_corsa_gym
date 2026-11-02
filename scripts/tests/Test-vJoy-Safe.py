#!/usr/bin/env python3
"""Test prudent de vJoy Device 1 sans Assetto Corsa."""

import sys
import time
from pathlib import Path

VJOY_DLL = r"C:\Program Files\vJoy\x64\vJoyInterface.dll"
SCALE = 16384
DEVICE = 1

PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent
    / "assetto_corsa_gym"
    / "AssettoCorsaPlugin"
    / "plugins"
    / "sensors_par"
)
sys.path.insert(0, str(PLUGIN_DIR))

from vjoy import vJoy  # noqa: E402


def x_pos(steer: float) -> int:
    """steer 1.0 = centre, <1 gauche, >1 droite (meme echelle que car_control)."""
    return int(steer * SCALE)


def y_pos(fraction: float) -> int:
    """fraction 0..0.5 -> axe Y (max test 50 %)."""
    return int(fraction * 2 * SCALE)


def z_pos(fraction: float) -> int:
    """fraction 0..0.5 -> axe Z (max test 50 %)."""
    return int(fraction * 2 * SCALE)


def attendre(etape: str) -> None:
    input(f"\n[{etape}] Appuyez sur Entree pour continuer...")


def appliquer(vj: vJoy, *, x: float = 1.0, y: float = 0.0, z: float = 0.0, boutons: int = 0) -> None:
    pos = vj.generateJoystickPosition(
        wAxisX=x_pos(x),
        wAxisY=y_pos(y),
        wAxisZ=z_pos(z),
        lButtons=boutons,
    )
    if not vj.update(pos):
        raise RuntimeError("UpdateVJD a echoue")


def neutre(vj: vJoy) -> None:
    appliquer(vj, x=1.0, y=0.0, z=0.0, boutons=0)


def main() -> int:
    if not Path(VJOY_DLL).is_file():
        print(f"ERREUR : DLL introuvable : {VJOY_DLL}")
        return 1

    print("Test vJoy securise - Device 1 uniquement")
    print("Aucun jeu lance. Etat neutre par defaut : X centre, Y/Z zero, boutons relaches.")

    vj = vJoy(reference=DEVICE)
    if not vj.open():
        print(f"ERREUR : impossible d'acquerir vJoy Device {DEVICE}.")
        return 1

    try:
        neutre(vj)
        print("Peripherique acquis. Etat neutre applique.")

        attendre("1/8 - X legerement a gauche")
        appliquer(vj, x=0.92)
        print("X = gauche leger (steer 0.92)")

        attendre("2/8 - X au centre")
        neutre(vj)
        print("X = centre")

        attendre("3/8 - X legerement a droite")
        appliquer(vj, x=1.08)
        print("X = droite leger (steer 1.08)")

        attendre("4/8 - X au centre")
        neutre(vj)
        print("X = centre")

        attendre("5/8 - Y a 50 %")
        appliquer(vj, y=0.5)
        print("Y = 50 %")
        attendre("5/8 - Y a zero")
        appliquer(vj, y=0.0)
        print("Y = zero")

        attendre("6/8 - Z a 50 %")
        appliquer(vj, z=0.5)
        print("Z = 50 %")
        attendre("6/8 - Z a zero")
        appliquer(vj, z=0.0)
        print("Z = zero")

        attendre("7/8 - Bouton 1 brievement")
        appliquer(vj, boutons=0x00000001)
        time.sleep(0.3)
        appliquer(vj, boutons=0)
        print("Bouton 1 relache")

        attendre("8/8 - Bouton 2 brievement")
        appliquer(vj, boutons=0x00000002)
        time.sleep(0.3)
        appliquer(vj, boutons=0)
        print("Bouton 2 relache")

        print("\nTest termine avec succes.")
        return 0

    except KeyboardInterrupt:
        print("\nInterruption utilisateur.")
        return 130
    except Exception as exc:
        print(f"\nERREUR : {exc}")
        return 1
    finally:
        try:
            neutre(vj)
        except Exception:
            pass
        vj.close()
        print("Etat neutre restaure. Peripherique libere.")


if __name__ == "__main__":
    raise SystemExit(main())
