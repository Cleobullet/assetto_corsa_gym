"""Neutral-to-first gear shift via vJoy (C4D collector)."""
from __future__ import annotations

import configparser
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Telemetry convention used across gym scripts (+1 offset: N=1, 1st=2).
GEAR_NEUTRAL = 1
GEAR_FIRST = 2
GEAR_NEUTRAL_RAW = 0
GEAR_FIRST_RAW = 1

SHIFT_HOLD_SEC = 0.15
SHIFT_RESPOND_HZ = 25
GEAR_SHIFT_WAIT_SEC = 2.0
MAX_SHIFT_ATTEMPTS = 3
FRESH_SAMPLES_REQUIRED = 2


def gear_raw(state) -> int:
    return int(state.get("actualGear", state.get("gear", -1)))


def is_neutral(gear: int) -> bool:
    return gear in (GEAR_NEUTRAL, GEAR_NEUTRAL_RAW)


def is_first(gear: int) -> bool:
    g = int(gear)
    if g == GEAR_NEUTRAL:
        return False
    return g in (GEAR_FIRST, GEAR_FIRST_RAW)


def vjoy_device_id() -> int:
    return int(os.environ.get("ACGYM_VJOY_DEVICE", "1"))


def _parse_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.optionxform = str  # preserve case
    parser.read(path, encoding="utf-8-sig")
    return parser


def _ini_int(value: str | None, default: int = -1) -> int:
    if value is None:
        return default
    text = str(value).split(";", 1)[0].strip()
    if not text or text.lower() == "null":
        return default
    try:
        return int(text, 0)
    except ValueError:
        return default


def _section_dict(cfg: configparser.ConfigParser, section: str) -> dict[str, str]:
    if not cfg.has_section(section):
        return {}
    return {key: cfg.get(section, key, fallback="") for key in cfg.options(section)}


def ac_documents_candidates() -> list[Path]:
    paths: list[Path] = []
    env_path = os.environ.get("AC_DOCUMENTS_PATH")
    if env_path:
        paths.append(Path(env_path))
    home = Path.home()
    paths.append(home / "Documents" / "Assetto Corsa")
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        paths.append(Path(onedrive) / "Documents" / "Assetto Corsa")
    paths.append(home / "OneDrive" / "Documents" / "Assetto Corsa")
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            out.append(path)
    return out


def worker_profile_root(worker_id: int) -> Path:
    return REPO_ROOT / "ac_profiles" / "workers" / ("worker_{0}".format(worker_id))


def vjoy_preset_name(worker_id: int) -> str:
    return "Vjoy_W{0}.ini".format(worker_id)


def ac_button_to_vjoy(ac_button_raw: int) -> tuple[int, int]:
    """AC INI BUTTON is 0-based; vJoy lButtons bit and 1-based button number."""
    btn = max(0, int(ac_button_raw))
    return 1 << btn, btn + 1


_CONTROLS_SECTIONS_FROM_PRESET = (
    "CONTROLLERS",
    "STEER",
    "THROTTLE",
    "BRAKES",
    "CLUTCH",
    "GEARUP",
    "GEARDN",
    "SHIFTER",
)

VJOY_DEVICE_NAME = "vJoy Device"
DEFAULT_VJOY_PGUID = "4C035200-7051-11EB-8002-444553540000"
_JOY_BINDING_SECTIONS = (
    "STEER",
    "THROTTLE",
    "BRAKES",
    "CLUTCH",
    "GEARUP",
    "GEARDN",
    "HANDBRAKE",
)

# Detected (NOT assumed) vJoy device_id -> joystick enumeration index. Written by
# ac_workers.vjoy_index_probe.detect_and_save_mapping at session start. The whole
# point of the fix: AC binds a car to "JOY=<enumeration index>", and that index is
# NOT guaranteed to equal device_id-1, so we must use the empirically detected one.
VJOY_MAPPING_PATH = REPO_ROOT / "outputs" / "diagnostics" / "vjoy_mapping_latest.json"


class VjoyIndexMappingError(RuntimeError):
    """Raised when the detected device->JOY-index mapping is missing for a device.

    This is a hard guard rail: without a trustworthy mapping we refuse to patch
    controls.ini, because guessing JOY=worker_id is exactly the bug that wires a
    worker to the wrong car.
    """


def detected_joy_index_for_device(device_id: int) -> int | None:
    """Read the persisted device_id -> joystick enumeration index (or None)."""
    import json as _json

    try:
        if not VJOY_MAPPING_PATH.is_file():
            return None
        data = _json.loads(VJOY_MAPPING_PATH.read_text(encoding="utf-8"))
        mapping = data.get("device_to_index") or {}
        value = mapping.get(str(int(device_id)))
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError):
        return None


def patch_ini_sections(path: Path, patches: dict[str, dict[str, str]]) -> list[str]:
    """Update INI keys in-place; preserve unrelated sections and keys."""
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_text(encoding="utf-8-sig")
    lines = raw.splitlines(keepends=True)
    if not lines and not raw.endswith("\n"):
        lines = [""]

    section_ranges: dict[str, tuple[int, int]] = {}
    current = ""
    start = 0
    for idx, line in enumerate(lines):
        trim = line.strip()
        if trim.startswith("[") and trim.endswith("]"):
            if current:
                section_ranges[current] = (start, idx)
            current = trim[1:-1].strip()
            start = idx + 1
    if current:
        section_ranges[current] = (start, len(lines))

    changed: list[str] = []

    def _format_key_line(key: str, value: str) -> str:
        return "{0}={1}\n".format(key, value)

    for section, updates in patches.items():
        if not updates:
            continue
        if section not in section_ranges:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
            lines.append("[{0}]\n".format(section))
            for key, value in updates.items():
                lines.append(_format_key_line(key, value))
                changed.append("{0}.{1}".format(section, key))
            section_ranges[section] = (len(lines) - len(updates), len(lines))
            continue

        sec_start, sec_end = section_ranges[section]
        existing: dict[str, int] = {}
        for idx in range(sec_start, sec_end):
            trim = lines[idx].strip()
            if not trim or trim.startswith(";") or trim.startswith("#") or "=" not in trim:
                continue
            key = trim.split("=", 1)[0].strip()
            existing[key] = idx

        insert_at = sec_end
        for key, value in updates.items():
            label = "{0}.{1}".format(section, key)
            if key in existing:
                old_line = lines[existing[key]]
                old_val = old_line.split("=", 1)[1].strip() if "=" in old_line else ""
                new_val = str(value).strip()
                if old_val != new_val:
                    prefix = old_line.split("=", 1)[0]
                    suffix = "\n" if old_line.endswith("\n") else ""
                    lines[existing[key]] = prefix + "=" + new_val + suffix
                    changed.append(label)
            else:
                lines.insert(insert_at, _format_key_line(key, value))
                insert_at += 1
                for name, bounds in list(section_ranges.items()):
                    sec_s, sec_e = bounds
                    if sec_s >= insert_at - 1:
                        section_ranges[name] = (sec_s + 1, sec_e + 1)
                    elif sec_e >= insert_at - 1 and name != section:
                        section_ranges[name] = (sec_s, sec_e + 1)
                section_ranges[section] = (sec_start, section_ranges[section][1] + 1)
                changed.append(label)

    path.write_text("".join(lines), encoding="utf-8")
    return changed


def sync_controls_shift_bindings_from_preset(controls_path: Path, preset_path: Path) -> list[str]:
    """Merge vJoy driving/shift bindings from worker preset into live controls.ini."""
    if not preset_path.is_file():
        raise FileNotFoundError(preset_path)
    preset_cfg = _parse_ini(preset_path)
    patches: dict[str, dict[str, str]] = {}

    header = _section_dict(preset_cfg, "HEADER")
    if header.get("INPUT_METHOD"):
        patches["HEADER"] = {"INPUT_METHOD": header["INPUT_METHOD"]}

    for section in _CONTROLS_SECTIONS_FROM_PRESET:
        if not preset_cfg.has_section(section):
            continue
        section_values = _section_dict(preset_cfg, section)
        if section in ("GEARUP", "GEARDN"):
            joy = _ini_int(section_values.get("JOY"))
            if joy >= 0:
                section_values = dict(section_values)
                section_values["KEY"] = "-1"
        patches[section] = section_values

    return patch_ini_sections(controls_path, patches)


def patch_controls_vjoy_joy_index(
    controls_path: Path, worker_id: int, *, joy_index: int | None = None
) -> list[str]:
    """Point controls.ini at the DETECTED joystick index for this worker's device.

    Worker N always writes to vJoy device N+1, but AC binds a car by JOY=<joystick
    enumeration index>, and that index is NOT guaranteed to equal N (Windows can
    enumerate vJoy devices in any order). We therefore use the empirically detected
    ``device_id -> joystick index`` map. If ``joy_index`` is not supplied it is read
    from the persisted detection; a missing entry is a hard error (guard rail).
    """
    device_id = int(worker_id) + 1
    if joy_index is None:
        joy_index = detected_joy_index_for_device(device_id)
    if joy_index is None:
        raise VjoyIndexMappingError(
            "no detected JOY index for vJoy device {0} (worker {1}). Run "
            "`python -m ac_workers.vjoy_index_probe --save` with all vJoy devices free "
            "before launch; refusing to bind controls.ini by assumption.".format(
                device_id, worker_id
            )
        )
    joy_index = max(0, int(joy_index))
    if not controls_path.is_file():
        raise FileNotFoundError(controls_path)

    cfg = _parse_ini(controls_path)
    controllers = _section_dict(cfg, "CONTROLLERS")
    pguid = controllers.get("PGUID0", DEFAULT_VJOY_PGUID).split(";", 1)[0].strip()
    if not pguid:
        pguid = DEFAULT_VJOY_PGUID

    controller_updates: dict[str, str] = {}
    for i in range(joy_index + 1):
        controller_updates["CON{0}".format(i)] = VJOY_DEVICE_NAME
        controller_updates["PGUID{0}".format(i)] = pguid

    section_patches: dict[str, dict[str, str]] = {"CONTROLLERS": controller_updates}
    for section in _JOY_BINDING_SECTIONS:
        if not cfg.has_section(section):
            continue
        section_values = _section_dict(cfg, section)
        if _ini_int(section_values.get("JOY")) < 0:
            continue
        updates = dict(section_values)
        updates["JOY"] = str(joy_index)
        if section == "STEER":
            # Without MIN/MAX, AC treats the steer axis as 0..1 (right only).
            # vJoy is bipolar (0=left, 16384=center, 32767=right).
            updates["MIN"] = "-1"
            updates["MAX"] = "1"
        section_patches[section] = updates

    return patch_ini_sections(controls_path, section_patches)


def sync_assists_from_preset(assists_path: Path, preset_path: Path) -> list[str]:
    """Force AC's auto gearbox on for AI workers.

    The RL policy action space is 3D (steer/throttle/brake) and never commands a
    gear change during driving, so in-race upshifts/downshifts rely entirely on
    Assetto Corsa's AUTO_SHIFTER assist. The recorded training data is all
    auto_shift=True. AUTO_SHIFTER must therefore stay 1 regardless of the preset's
    SHIFTER mode, otherwise the car is stuck in first gear for the whole episode.
    """
    return patch_ini_sections(assists_path, {"ASSISTS": {"AUTO_SHIFTER": "1"}})


def prepare_worker_documents_controls(worker_id: int, documents_root: Path | str) -> dict[str, Any]:
    """After profile copy: bind controls.ini GEARUP to the worker vJoy preset."""
    root = Path(documents_root)
    preset_path = root / "cfg" / "controllers" / "savedsetups" / vjoy_preset_name(worker_id)
    controls_path = root / "cfg" / "controls.ini"
    assists_path = root / "cfg" / "assists.ini"
    sync_controls_shift_bindings_from_preset(controls_path, preset_path)
    patch_controls_vjoy_joy_index(controls_path, worker_id)
    if assists_path.is_file():
        sync_assists_from_preset(assists_path, preset_path)
    # Capture the exact controls.ini this worker just finalized, BEFORE its AC
    # instance launches. controls.ini is shared and rewritten per worker, so this
    # snapshot is the only trustworthy per-worker record of the JOY index actually
    # written. Best-effort: a snapshot failure must never block the launch.
    try:
        from ac_workers.controls_snapshot import write_snapshot

        detected = detected_joy_index_for_device(int(worker_id) + 1)
        write_snapshot(worker_id, controls_path, detected)
    except Exception:
        logger.warning("controls snapshot failed worker=%s", worker_id, exc_info=True)
    prev_docs = os.environ.get("AC_DOCUMENTS_PATH")
    os.environ["AC_DOCUMENTS_PATH"] = str(root)
    try:
        return resolve_shift_up_mapping(worker_id)
    finally:
        if prev_docs is None:
            os.environ.pop("AC_DOCUMENTS_PATH", None)
        else:
            os.environ["AC_DOCUMENTS_PATH"] = prev_docs


def resolve_shift_up_mapping(worker_id: int | None = None) -> dict[str, Any]:
    """Read-only: resolve effective GEARUP binding from live Documents + worker profile."""
    wid = worker_id if worker_id is not None else int(os.environ.get("ACGYM_WORKER_ID", "0"))
    preset_name = vjoy_preset_name(wid)
    report: dict[str, Any] = {
        "worker_id": wid,
        "active_preset_path": "",
        "controls_ini_path": "",
        "assists_ini_path": "",
        "shift_up_section": "GEARUP",
        "configured_joy": -1,
        "configured_button_raw": -1,
        "vjoy_device_id": vjoy_device_id(),
        "vjoy_button_number": -1,
        "vjoy_button_mask": 0,
        "gearbox_mode": "unknown",
        "auto_clutch": None,
        "auto_shifter": None,
        "controls_gearup_joy": -1,
        "controls_gearup_button_raw": -1,
        "controls_con0": "",
        "valid": False,
        "error": None,
    }

    preset_path: Path | None = None
    controls_path: Path | None = None
    assists_path: Path | None = None

    search_roots: list[Path] = []
    # The worker's OWN Documents copy (the registry-redirect target, created by
    # Redirect-AcDocumentsToWorker) comes FIRST, so the vJoy prehold / gear check
    # reads the per-worker controls.ini (which has this worker's CON<N> + JOY), not
    # the shared Documents (which only carries the last-launched worker's binding).
    worker_docs = worker_profile_root(wid) / "Documents" / "Assetto Corsa"
    if worker_docs.is_dir():
        search_roots.append(worker_docs)
    search_roots.extend(ac_documents_candidates())
    prof = worker_profile_root(wid)
    if prof.is_dir():
        search_roots.append(prof)

    for root in search_roots:
        candidate = root / "cfg" / "controllers" / "savedsetups" / preset_name
        if candidate.is_file() and preset_path is None:
            preset_path = candidate
            report["active_preset_path"] = str(candidate)
        ctrl = root / "cfg" / "controls.ini"
        if ctrl.is_file() and controls_path is None:
            controls_path = ctrl
            report["controls_ini_path"] = str(ctrl)
        assist = root / "cfg" / "assists.ini"
        if assist.is_file() and assists_path is None:
            assists_path = assist
            report["assists_ini_path"] = str(assist)

    if preset_path is None or not preset_path.is_file():
        report["error"] = "preset missing: {0}".format(preset_name)
        return report

    preset_cfg = _parse_ini(preset_path)
    preset_gear = _section_dict(preset_cfg, "GEARUP")
    preset_joy = _ini_int(preset_gear.get("JOY"))
    preset_btn = _ini_int(preset_gear.get("BUTTON"))
    report["configured_joy"] = preset_joy
    report["configured_button_raw"] = preset_btn

    shifter = _section_dict(preset_cfg, "SHIFTER")
    if shifter.get("ACTIVE") == "1":
        report["gearbox_mode"] = "h_pattern"
    else:
        report["gearbox_mode"] = "sequential"

    if assists_path and assists_path.is_file():
        assists_cfg = _parse_ini(assists_path)
        assists = _section_dict(assists_cfg, "ASSISTS")
        report["auto_clutch"] = assists.get("AUTO_CLUTCH") == "1"
        report["auto_shifter"] = assists.get("AUTO_SHIFTER") == "1"

    controllers: dict[str, str] = {}
    if controls_path and controls_path.is_file():
        controls_cfg = _parse_ini(controls_path)
        controllers = _section_dict(controls_cfg, "CONTROLLERS")
        report["controls_con0"] = controllers.get("CON0", "")
        ctrl_gear = _section_dict(controls_cfg, "GEARUP")
        report["controls_gearup_joy"] = _ini_int(ctrl_gear.get("JOY"))
        report["controls_gearup_button_raw"] = _ini_int(ctrl_gear.get("BUTTON"))

    if preset_joy < 0 or preset_btn < 0:
        report["error"] = (
            "preset {0} GEARUP unbound (JOY={1} BUTTON={2})".format(
                preset_path, preset_joy, preset_btn
            )
        )
        return report

    mask, btn_num = ac_button_to_vjoy(preset_btn)
    report["vjoy_button_mask"] = mask
    report["vjoy_button_number"] = btn_num

    controls_vjoy_ok = (
        report["controls_gearup_joy"] >= 0
        and report["controls_gearup_button_raw"] >= 0
    )
    # The expected controls.ini JOY index is the DETECTED enumeration index for this
    # worker's vJoy device (device = wid + 1), not the worker id. Fall back to wid
    # only if no detection exists (then controls_matches will flag a mismatch).
    detected_idx = detected_joy_index_for_device(wid + 1)
    expected_controls_joy = detected_idx if detected_idx is not None else wid
    report["detected_joy_index"] = detected_idx
    report["expected_controls_joy"] = expected_controls_joy
    controls_matches = (
        controls_vjoy_ok
        and report["controls_gearup_joy"] == expected_controls_joy
        and report["controls_gearup_button_raw"] == preset_btn
    )
    con0_vjoy = "vjoy" in str(report["controls_con0"]).lower()
    worker_con_key = "CON{0}".format(expected_controls_joy)
    worker_con = controllers.get(worker_con_key, "")
    worker_con_vjoy = "vjoy" in str(worker_con).lower()

    if not controls_vjoy_ok:
        report["error"] = (
            "controls.ini ({0}) GEARUP not on vJoy (JOY={1} BUTTON={2}); "
            "preset ({3}) expects JOY={4} BUTTON={5} (vJoy button {6}, mask 0x{7:08x})".format(
                report["controls_ini_path"],
                report["controls_gearup_joy"],
                report["controls_gearup_button_raw"],
                report["active_preset_path"],
                preset_joy,
                preset_btn,
                btn_num,
                mask,
            )
        )
        return report

    if not controls_matches:
        # controls.ini is shared across workers and gets overwritten by the next
        # worker's profile. This is expected during sequential scale-test launches.
        # Trust the preset (per-worker, immutable) instead of the volatile controls.ini.
        import logging as _log
        _log.getLogger(__name__).warning(
            "controls.ini JOY=%s != expected %s for worker %s (shared Documents race); "
            "using preset binding instead",
            report["controls_gearup_joy"], expected_controls_joy, wid,
        )

    if not worker_con_vjoy:
        report["error"] = (
            "controls.ini ({0}) {1}={2!r} does not reference vJoy Device; worker={3}".format(
                report["controls_ini_path"],
                worker_con_key,
                worker_con,
                wid,
            )
        )
        return report

    if not con0_vjoy:
        report["error"] = (
            "controls.ini ({0}) CON0={1!r} does not reference vJoy Device; preset={2}".format(
                report["controls_ini_path"],
                report["controls_con0"],
                report["active_preset_path"],
            )
        )
        return report

    report["valid"] = True
    report["error"] = None
    logger.info(
        "gear_mapping: preset=%s GEARUP JOY=%s BUTTON=%s -> vJoy device=%s button=%s mask=0x%08x "
        "gearbox=%s auto_clutch=%s auto_shifter=%s",
        preset_path,
        preset_joy,
        preset_btn,
        report["vjoy_device_id"],
        btn_num,
        mask,
        report["gearbox_mode"],
        report["auto_clutch"],
        report["auto_shifter"],
    )
    return report


def read_auto_shifter_active(assists_ini_path: str | None) -> bool | None:
    """Read AUTO_SHIFTER from the assists.ini path resolved in mapping."""
    if not assists_ini_path:
        return None
    path = Path(assists_ini_path)
    if not path.is_file():
        return None
    assists = _section_dict(_parse_ini(path), "ASSISTS")
    value = assists.get("AUTO_SHIFTER")
    if value is None:
        return None
    return str(value).split(";", 1)[0].strip() == "1"


def _apply_vjoy_button(client, mask: int) -> None:
    lc = client.controls.local_controls
    lc.onButtons = int(mask)
    lc.update()


def _release_vjoy_buttons(client) -> None:
    lc = client.controls.local_controls
    lc.onButtons = 0
    lc.set_controls(
        steer=0.0,
        acc=-1.0,
        brake=-1.0,
        enable_gear_shift=False,
        shift_up=False,
        shift_down=False,
    )


def pulse_shift_up(
    client,
    mapping: dict[str, Any],
    hold_sec: float = SHIFT_HOLD_SEC,
    *,
    acc: float = -1.0,
    steer: float = 0.0,
) -> None:
    mask = int(mapping.get("vjoy_button_mask", 0))
    btn_num = int(mapping.get("vjoy_button_number", -1))
    device = int(mapping.get("vjoy_device_id", vjoy_device_id()))
    logger.info(
        "auto_shift: vJoy device=%s shift button=%s (raw=%s) mask=0x%08x preset=%s",
        device,
        btn_num,
        mapping.get("configured_button_raw"),
        mask,
        mapping.get("active_preset_path"),
    )
    try:
        client.controls.set_controls(
            steer=float(steer),
            acc=float(acc),
            brake=-1.0,
            enable_gear_shift=True,
            shift_up=False,
            shift_down=False,
        )
        _apply_vjoy_button(client, mask)
        client.respond_to_server()
        logger.info("auto_shift: button down")
        interval = 1.0 / max(SHIFT_RESPOND_HZ, 1)
        deadline = time.time() + hold_sec
        while time.time() < deadline:
            _apply_vjoy_button(client, mask)
            client.respond_to_server()
            time.sleep(interval)
    finally:
        _release_vjoy_buttons(client)
        client.respond_to_server()
        logger.info("auto_shift: button up")


def neutralize_controls(client) -> None:
    _release_vjoy_buttons(client)
    try:
        client.respond_to_server()
    except Exception:
        pass


def auto_shift_neutral_to_first(env) -> tuple[bool, dict[str, Any]]:
    hook = getattr(env, "_collector_phase_hook", None)
    if hook:
        hook("gear_initialization")

    mapping = getattr(env, "_gear_shift_mapping", None)
    if mapping is None:
        mapping = resolve_shift_up_mapping()
        env._gear_shift_mapping = mapping

    report: dict[str, Any] = {
        "vjoy_device": mapping.get("vjoy_device_id", vjoy_device_id()),
        "shift_button": mapping.get("vjoy_button_number"),
        "shift_mask": mapping.get("vjoy_button_mask"),
        "configured_button_raw": mapping.get("configured_button_raw"),
        "active_preset_path": mapping.get("active_preset_path"),
        "controls_ini_path": mapping.get("controls_ini_path"),
        "expected_first_gear": GEAR_FIRST,
        "expected_neutral_gears": [GEAR_NEUTRAL, GEAR_NEUTRAL_RAW],
        "gearbox_mode": mapping.get("gearbox_mode"),
        "auto_clutch": mapping.get("auto_clutch"),
        "auto_shifter": mapping.get("auto_shifter"),
        "attempts": [],
        "error": None,
    }

    if not mapping.get("valid"):
        report["error"] = mapping.get("error") or "gear mapping invalid"
        logger.error("auto_shift: %s", report["error"])
        if hook:
            hook("gear_mapping_invalid", report["error"])
        neutralize_controls(env.client)
        return False, report

    if not env.states:
        report["error"] = "no telemetry state after reset flush"
        logger.error("auto_shift: %s", report["error"])
        return False, report

    auto_shifter = mapping.get("auto_shifter")
    fresh_auto = read_auto_shifter_active(mapping.get("assists_ini_path"))
    if fresh_auto is not None:
        auto_shifter = fresh_auto
        report["auto_shifter"] = fresh_auto

    gear_before = gear_raw(env.states[-1])
    report["gear_raw_before"] = gear_before

    if auto_shifter:
        # AUTO_SHIFTER only engages 1st once it sees real throttle. A fresh or
        # random policy often outputs ~0 throttle and stays in neutral forever.
        # Hold ~75% throttle until the gearbox leaves neutral, then hand control
        # back. Retry a few times: engagement is timing-dependent (race clutch).
        logger.info("auto_shift: AUTO_SHIFTER=1, throttle-boosting to engage first gear")
        boost_acc = 0.5  # pedal_cmd 0.5 -> 75% throttle after normalization
        gear_after = gear_before
        # AC gear convention: 0=neutral, 1=REVERSE, 2=first, 3=second, ...
        # The legacy constants mislabel raw=1 as "neutral", but AUTO_SHIFTER will
        # NOT shift R->1st on throttle alone (it just drives backwards). Shift up
        # once to reach neutral before the throttle boost can engage first.
        if gear_before == 1:
            try:
                pulse_shift_up(env.client, mapping)
                time.sleep(SHIFT_HOLD_SEC)
                gear_after = gear_raw(env.client.step_sim())
            except Exception:
                gear_after = gear_before
            logger.info("auto_shift: reverse (raw=1) detected, shifted to raw=%s", gear_after)
        for attempt in range(1, MAX_SHIFT_ATTEMPTS + 1):
            fresh = 0
            try:
                deadline = time.time() + GEAR_SHIFT_WAIT_SEC
                while time.time() < deadline:
                    env.client.controls.set_controls(steer=0.0, acc=boost_acc, brake=-1.0)
                    env.client.respond_to_server()
                    try:
                        sim_state = env.client.step_sim()
                    except Exception as exc:
                        report["error"] = "telemetry lost: {0}".format(exc)
                        logger.error("auto_shift: %s", report["error"])
                        return False, report
                    g = gear_raw(sim_state)
                    fresh += 1
                    gear_after = g
                    if is_first(g):
                        break
                    time.sleep(1.0 / max(SHIFT_RESPOND_HZ, 1))
                if is_first(gear_after):
                    logger.info(
                        "auto_shift: first engaged via AUTO_SHIFTER throttle boost raw=%s (attempt %s, %s samples)",
                        gear_after,
                        attempt,
                        fresh,
                    )
                    report["gear_raw_after"] = gear_after
                    report["throttle_boost_attempts"] = attempt
                    report["throttle_boost_samples"] = fresh
                    return True, report
                logger.warning(
                    "auto_shift: AUTO_SHIFTER boost attempt %s did not engage first, gear raw=%s",
                    attempt,
                    gear_after,
                )
            finally:
                neutralize_controls(env.client)
        report["gear_raw_after"] = gear_after
        logger.warning(
            "auto_shift: throttle boost failed gear raw=%s — falling back to manual shift",
            gear_after,
        )
        # Do NOT give up here: pulse the shift button directly instead of relying
        # on AUTO_SHIFTER's throttle timing. Fall through to the manual path below.
        gear_before = gear_after

    logger.info(
        "auto_shift: gear raw before=%s expected N in %s first in %s",
        gear_before,
        report["expected_neutral_gears"],
        [GEAR_FIRST, GEAR_FIRST_RAW],
    )

    if is_first(gear_before):
        logger.info("auto_shift: already in first (raw=%s)", gear_before)
        report["gear_raw_after"] = gear_before
        return True, report

    if not is_neutral(gear_before):
        report["error"] = "unexpected gear raw={0}".format(gear_before)
        logger.error("auto_shift: %s", report["error"])
        return False, report

    for attempt in range(1, MAX_SHIFT_ATTEMPTS + 1):
        attempt_info: dict[str, Any] = {"attempt": attempt, "samples": []}
        logger.info("auto_shift: shift attempt %s/%s", attempt, MAX_SHIFT_ATTEMPTS)
        pulse_shift_up(env.client, mapping)

        deadline = time.time() + GEAR_SHIFT_WAIT_SEC
        gear_after = gear_before
        fresh = 0
        while time.time() < deadline:
            try:
                sim_state = env.client.step_sim()
            except Exception as exc:
                report["error"] = "telemetry lost: {0}".format(exc)
                logger.error("auto_shift: %s", report["error"])
                attempt_info["error"] = report["error"]
                report["attempts"].append(attempt_info)
                neutralize_controls(env.client)
                return False, report
            g = gear_raw(sim_state)
            attempt_info["samples"].append(g)
            fresh += 1
            gear_after = g
            logger.info("auto_shift: gear raw after sample=%s", g)
            if is_first(g):
                break

        attempt_info["gear_raw_after"] = gear_after
        attempt_info["fresh_samples"] = fresh
        report["attempts"].append(attempt_info)
        report["gear_raw_after"] = gear_after

        if is_first(gear_after):
            logger.info(
                "auto_shift: first engaged raw=%s after attempt %s (%s samples)",
                gear_after,
                attempt,
                fresh,
            )
            return True, report

        logger.warning(
            "auto_shift: attempt %s failed gear still raw=%s (samples=%s)",
            attempt,
            gear_after,
            fresh,
        )

    neutralize_controls(env.client)
    report["error"] = "timeout gear raw={0} after {1} attempts".format(
        report.get("gear_raw_after"),
        MAX_SHIFT_ATTEMPTS,
    )
    logger.error("auto_shift: %s", report["error"])
    return False, report


def verify_first_before_driving(client, mapping, max_attempts=3, clock=time.monotonic):
    """Engage first and confirm it on two fresh packets; never trust AUTO_SHIFTER alone."""
    report = {"samples": [], "attempts": 0, "error": "", "discarded_packets": 0}
    def fresh_state():
        drain = getattr(client, "discard_pending_telemetry", None)
        if callable(drain):
            report["discarded_packets"] += drain()
        return client.step_sim()
    previous_stamp = None
    streak = 0
    for attempt in range(max_attempts):
        report["attempts"] = attempt + 1
        # Refresh before deciding to shift: never act on the loading-screen gear.
        if callable(getattr(client, "discard_pending_telemetry", None)):
            fresh_state()
        current = gear_raw(client.state)
        if is_neutral(current):
            pulse_shift_up(client, mapping, acc=-1.0, steer=0.0)
        elif not is_first(current):
            report["error"] = "unexpected_start_gear:{0}".format(current)
            return False, report
        # The pulse itself also queues packets; discard them before confirmation.
        drain = getattr(client, "discard_pending_telemetry", None)
        if callable(drain):
            report["discarded_packets"] += drain()
        previous_stamp = None
        streak = 0
        deadline = clock() + GEAR_SHIFT_WAIT_SEC
        for _ in range(100):
            if clock() >= deadline:
                break
            state = client.step_sim()
            stamp = state.get("timestamp_ac")
            gear = gear_raw(state)
            report["samples"].append(gear)
            if stamp is None or stamp == previous_stamp:
                continue
            if previous_stamp is not None and stamp < previous_stamp:
                streak = 0
            previous_stamp = stamp
            streak = streak + 1 if is_first(gear) else 0
            if streak >= FRESH_SAMPLES_REQUIRED:
                return True, report
    report["error"] = "first_gear_not_confirmed"
    logger.error("first gear validation failed: %s", report)
    return False, report
