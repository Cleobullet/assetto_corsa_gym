#!/usr/bin/env python3
"""Smoke test officiel Assetto Corsa Gym — connexion, observation, action minimale, reset."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from omegaconf import OmegaConf

REPO_ROOT = Path(__file__).resolve().parents[2]
AI_ACS = REPO_ROOT / "AssettoCorsa-AI" / "assettocorsa" / "acs.exe"
GYM_ROOT = REPO_ROOT / "assetto_corsa_gym"
PLUGIN_DIR = GYM_ROOT / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
WORK_DIR = REPO_ROOT / "outputs" / "smoke_test"
DEFAULT_TRACK = "monza"
DEFAULT_CAR = "lotus_exos_125"
SPEED_LIMIT_KMH = 15.0
SAFE_ACTION = np.array([0.02, 0.0, 0.0], dtype=np.float32)
MAX_WALL_SEC = 25.0

sys.path.insert(0, str(GYM_ROOT))
sys.path.insert(0, str(PLUGIN_DIR))

import AssettoCorsaEnv.assettoCorsa as assettoCorsa  # noqa: E402

logger = logging.getLogger("Smoke-Test-AcGym")


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
        raise RuntimeError("acs.exe introuvable — lancez Hotlap depuis Content Manager AI.")
    ai_norm = normalize_path(str(AI_ACS))
    if not any(normalize_path(p) == ai_norm for p in paths):
        raise RuntimeError(
            "acs.exe actif ne provient pas de la copie AI.\n"
            f"Attendu : {AI_ACS}\n"
            f"Trouve  : {paths}"
        )
    logger.info("OK acs.exe AI detecte")


def check_speed(state: dict, label: str) -> None:
    speed_kmh = float(state.get("speed", 0.0)) * 3.6
    if speed_kmh > SPEED_LIMIT_KMH:
        raise RuntimeError(f"ARRET SECURITE [{label}] : vitesse {speed_kmh:.1f} km/h > {SPEED_LIMIT_KMH:.0f}")


def restore_neutral(client) -> None:
    if client is None:
        return
    try:
        client.controls.set_defaults()
        client.controls.apply_local_controls()
    except Exception as exc:
        logger.warning("Restauration neutre partielle : %s", exc)


def print_observation_summary(env, obs: np.ndarray, state: dict) -> None:
    logger.info("observation shape : %s  dtype=%s", obs.shape, obs.dtype)
    logger.info("state_dim config  : %s", env.state_dim)
    preview = np.array2string(obs[:8], precision=4, separator=", ")
    logger.info("obs[:8]           : %s", preview)
    logger.info(
        "telemetrie cle    : speed=%.2f m/s steer=%.3f acc=%.3f brake=%.3f RPM=%s gear=%s",
        float(state.get("speed", 0.0)),
        float(state.get("steerAngle", 0.0)),
        float(state.get("accStatus", 0.0)),
        float(state.get("brakeStatus", 0.0)),
        state.get("RPM", "?"),
        state.get("gear", "?"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test Assetto Corsa Gym (CPU, sans entrainement).")
    parser.add_argument("--config", default=str(REPO_ROOT / "legacy" / "configs" / "config.yml"), help="Fichier config OmegaConf")
    parser.add_argument("--track", default=DEFAULT_TRACK, help="AssettoCorsa.track")
    parser.add_argument("--car", default=DEFAULT_CAR, help="AssettoCorsa.car")
    parser.add_argument(
        "--skip-reset",
        action="store_true",
        help="Ne pas appeler simulation_management.send_reset (debug uniquement)",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    args = parse_args()
    started = time.time()
    env = None

    try:
        if time.time() - started > MAX_WALL_SEC:
            raise TimeoutError("delai global depasse")

        verify_ai_acs_running()

        cfg = OmegaConf.load(args.config)
        cfg.AssettoCorsa.screen_capture_enable = False
        cfg.AssettoCorsa.send_reset_at_start = True
        cfg.AssettoCorsa.recover_car_on_done = False
        cfg.AssettoCorsa.track = args.track
        cfg.AssettoCorsa.car = args.car

        logger.info(
            "Config session : track=%s car=%s port=%s",
            cfg.AssettoCorsa.track,
            cfg.AssettoCorsa.car,
            cfg.AssettoCorsa.ego_server_port,
        )

        WORK_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Initialisation AssettoCorsaEnv...")
        module_cfg = assettoCorsa.ModuleConfig(cfg.AssettoCorsa)
        import AssettoCorsaEnv.ac_env as ac_env  # noqa: E402

        env = ac_env.AssettoCorsaEnv(
            module_cfg.get_config(),
            output_path=str(WORK_DIR),
            ac_configs_path=module_cfg.ac_configs_path,
            max_episode_steps=module_cfg.max_episode_steps,
            save_observations=False,
        )

        logger.info("reset() — connexion UDP + observation complete...")
        obs = env.reset()
        state = env.states[-1]
        check_speed(state, "reset")
        print_observation_summary(env, obs, state)

        logger.info("step() action faible %s ...", SAFE_ACTION.tolist())
        obs_before = obs.copy()
        obs_after, _reward, done, _info = env.step(SAFE_ACTION)
        state = env.states[-1]
        check_speed(state, "step")
        print_observation_summary(env, obs_after, state)

        if np.allclose(obs_before, obs_after, rtol=0.0, atol=1e-9):
            raise RuntimeError("Observation identique apres step — la boucle Gym ne progresse pas.")
        logger.info("OK observation mise a jour apres action.")

        if done:
            logger.warning("done=True apres le premier step (inattendu en smoke test).")

        if not args.skip_reset:
            logger.info("recover_car() — reset officiel via port simulation_management...")
            env.recover_car()
            time.sleep(1.0)
            obs_reset, _reward, _done, _info = env.step(np.zeros(3, dtype=np.float32))
            state = env.states[-1]
            check_speed(state, "post-reset")
            logger.info("OK apres reset : speed=%.2f m/s", float(state.get("speed", 0.0)) * 3.6)

        elapsed = time.time() - started
        logger.info("SMOKE TEST REUSSI en %.1f s (CPU, sans entrainement).", elapsed)
        return 0

    except KeyboardInterrupt:
        logger.error("Interrompu par l'utilisateur.")
        return 130
    except Exception as exc:
        logger.error("ECHEC smoke test : %s", exc)
        return 1
    finally:
        if env is not None:
            restore_neutral(env.client)
            try:
                env.end()
            except Exception as exc:
                logger.warning("Fermeture env : %s", exc)
        logger.info("Neutre restaure, client deconnecte.")


if __name__ == "__main__":
    raise SystemExit(main())
