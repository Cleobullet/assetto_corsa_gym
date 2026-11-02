"""Driving-state detection and resume (AC main thread only)."""
import json
import logging
import os
import threading
import time

from sim_info import AC_LIVE, AC_OFF, AC_PAUSE, AC_REPLAY, info
from spawn_pose import spawn_pose

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_main_queue = []
_pending_resume_after_reset = False
_last_session_clock_ms = None
_last_session_clock_wall = 0.0
_cached_state = {}
_resume_attempts = 0
_last_auto_resume_wall = 0.0

SESSION_CLOCK_MIN_ADVANCE_MS = 1
SESSION_CLOCK_SAMPLE_SEC = 0.25
AUTO_RESUME_INTERVAL_SEC = 0.5
MAX_AUTO_RESUME_ATTEMPTS = 24
# In AC's pre-drive session menu (the "Drive" page) the car is parked with the
# brake fully applied so it cannot roll. The instant the Drive button is clicked
# and the player takes control, the brake releases. While AC is LIVE in both
# states (status==AC_LIVE) and the lap clock can already tick in the menu (esp.
# HOTLAP), the pinned brake is the only signal that reliably tells the two apart.
MENU_BRAKE_HOLD_THRESHOLD = 0.5


_spawn_once_done = False
_spawn_driving_since = 0.0
SPAWN_DELAY_SEC = 3.0


def queue_reset_car():
    with _lock:
        _main_queue.append("reset")


def queue_spawn(fraction):
    with _lock:
        _main_queue.append(("spawn", float(fraction)))


def queue_resume_drive():
    with _lock:
        _main_queue.append("resume")


def _read_session_clock_ms():
    return int(info.graphics.iCurrentTime)


def _read_ac_status():
    return int(info.graphics.status)


def _read_physics_packet_id():
    # Per-tick counter AC increments every physics frame. Used purely as a
    # shared-memory fingerprint: if two AC instances report the same packetId at
    # the same instant, they are reading the same (global) shared memory.
    try:
        return int(info.physics.packetId)
    except Exception:
        return -1


def _read_brake():
    # Shared-memory fallback (thread-safe). Multi-instance AC shares one
    # acpmf_physics mapping, so this can be ANOTHER car's brake. Prefer
    # ac.getCarState from acUpdate (see evaluate_driving_state local_brake).
    try:
        return float(info.physics.brake)
    except Exception:
        return 0.0


def _read_local_brake():
    # Main-thread only (acUpdate / reset / resume). This instance's own car.
    try:
        import ac
        import acsys

        return float(ac.getCarState(0, acsys.CS.Brake))
    except Exception:
        return None


def _session_clock_advancing(now):
    global _last_session_clock_ms, _last_session_clock_wall
    clock = _read_session_clock_ms()
    if _last_session_clock_ms is None:
        _last_session_clock_ms = clock
        _last_session_clock_wall = now
        return False
    if now - _last_session_clock_wall < SESSION_CLOCK_SAMPLE_SEC:
        return False
    advanced = clock > (_last_session_clock_ms + SESSION_CLOCK_MIN_ADVANCE_MS)
    _last_session_clock_ms = clock
    _last_session_clock_wall = now
    return advanced


def _controls_live(controls):
    if controls is None:
        return None
    # In-game plugin is not the vJoy feeder; probing throttle here would AcquireVJD
    # every frame, race the collector, and creep the car forward.
    if hasattr(controls, "feeder_enabled") and not controls.feeder_enabled():
        return None
    try:
        before = float(info.physics.gas)
        controls.set_controls(0.0, 0.2, -1.0)
        after = float(info.physics.gas)
        controls.set_controls(0.0, -1.0, -1.0)
        return after > before + 0.01
    except Exception:
        return None


def evaluate_driving_state(controls=None, now=None, local_brake=None, in_ac_update=False):
    if now is None:
        now = time.time()
    status = _read_ac_status()
    if in_ac_update:
        # acUpdate is running on THIS instance: do not inherit another
        # instance's pause/off from the global acpmf_graphics mapping.
        status = AC_LIVE
    clock_ms = _read_session_clock_ms()
    clock_advancing = _session_clock_advancing(now)
    controls_live = _controls_live(controls)
    if local_brake is None:
        local_brake = _read_local_brake() if in_ac_update else None
    brake = float(local_brake) if local_brake is not None else _read_brake()
    menu_brake_hold = brake >= MENU_BRAKE_HOLD_THRESHOLD

    reasons = []
    if status == AC_PAUSE:
        reasons.append("ac_status_pause")
    if status == AC_OFF:
        reasons.append("ac_status_off")
    if status == AC_REPLAY:
        reasons.append("ac_status_replay")
    if status != AC_LIVE:
        reasons.append("ac_status_not_live")
    if menu_brake_hold:
        reasons.append("session_menu_brake_hold")
    if _pending_resume_after_reset and not clock_advancing:
        reasons.append("session_clock_frozen")

    if status == AC_PAUSE:
        driving = False
    elif menu_brake_hold:
        driving = False
    elif status != AC_LIVE:
        driving = False
    elif _pending_resume_after_reset:
        # After ext_resetCar the session clock can freeze briefly (multi-instance
        # load) while the car is already LIVE with brake released. Trust the same
        # physical gate as drive_truth instead of requiring clock_advancing here.
        driving = True
    elif controls_live is True:
        driving = True
    elif controls_live is False:
        driving = False
    else:
        # External collector feeds vJoy: LIVE + brake released = driving.
        driving = True

    return {
        "driving": driving,
        # Clean physical signals consumed by the single truth source
        # (ac_workers/drive_truth.py is_drive_really_active). sim_live + brake
        # are the only fields the collector trusts to separate menu from drive;
        # session_clock_advancing is kept for diagnostics only, NOT for truth.
        "sim_live": status == AC_LIVE,
        "ac_status": status,
        "physics_packet_id": _read_physics_packet_id(),
        "session_clock_ms": clock_ms,
        "session_clock_advancing": clock_advancing,
        "controls_live": controls_live,
        "physics_brake": brake,
        "menu_brake_hold": menu_brake_hold,
        "pending_resume_after_reset": _pending_resume_after_reset,
        "resume_attempts": _resume_attempts,
        "reason": ",".join(reasons) if reasons else "",
    }


def get_driving_state_json(controls=None):
    # acUpdate (main thread) owns controls; never call set_controls from the mgmt socket thread.
    with _lock:
        if _cached_state:
            snap = dict(_cached_state)
        else:
            snap = evaluate_driving_state(controls=None)
    return json.dumps(snap)


# EnumWindowsProc must return BOOL (4 bytes). ctypes.c_bool is 1 byte and can
# crash AC's Python 3.3 when the callback runs on the game thread.
_ENUM_WINDOWS_PROC = None


def _enum_windows_proc_type():
    global _ENUM_WINDOWS_PROC
    if _ENUM_WINDOWS_PROC is None:
        import ctypes
        from ctypes import wintypes

        _ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
    return _ENUM_WINDOWS_PROC


def _find_process_main_window():
    import ctypes
    import os
    from ctypes import wintypes

    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    pid = os.getpid()
    found = []

    @_enum_windows_proc_type()
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        wnd_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if int(wnd_pid.value) != int(pid):
            return True
        length = user32.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        title = buf.value or ""
        if "Assetto Corsa" in title:
            found.insert(0, int(hwnd))
        elif not found:
            found.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _post_key_to_main_window(vk):
    if os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = _find_process_main_window()
        if not hwnd:
            return False
        wm_keydown = 0x0100
        wm_keyup = 0x0101
        scan = user32.MapVirtualKeyW(vk, 0)
        lp_down = 1 | (scan << 16)
        lp_up = 1 | (scan << 16) | (1 << 30) | (1 << 31)
        user32.PostMessageW(hwnd, wm_keydown, vk, lp_down)
        user32.PostMessageW(hwnd, wm_keyup, vk, lp_up)
        return True
    except Exception:
        logger.exception("resume_drive: key post failed vk=%s", vk)
        return False


def _post_return_confirm():
    return _post_key_to_main_window(0x0D)


def _post_escape_confirm():
    return _post_key_to_main_window(0x1B)


def _client_rect_size(hwnd):
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    if not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return 832, 464
    w = max(1, int(rect.right - rect.left))
    h = max(1, int(rect.bottom - rect.top))
    return w, h


def _drive_wheel_points(hwnd):
    # 38,78 was 3 cm too high. +114 px (~3 cm at 96 DPI).
    w, h = _client_rect_size(hwnd)
    if w < 400 or h < 250:
        return []
    pts = []
    for x, y in ((38, 192), (48, 192), (38, 200), (48, 200)):
        if y < h and x < w:
            pts.append((x, y))
    return pts


def _post_client_click(hwnd, x, y):
    import ctypes

    user32 = ctypes.windll.user32
    lp = (int(y) << 16) | (int(x) & 0xFFFF)
    user32.PostMessageW(hwnd, 0x0200, 0, lp)
    user32.PostMessageW(hwnd, 0x0201, 1, lp)
    user32.PostMessageW(hwnd, 0x0202, 0, lp)
    return True


def _post_drive_wheel_clicks():
    # PostMessage only from plugin — the launcher handles real mouse clicks.
    # Never move the real cursor from the plugin: it interferes during gameplay.
    hwnd = _find_process_main_window()
    if not hwnd:
        return 0
    n = 0
    for x, y in _drive_wheel_points(hwnd):
        try:
            _post_client_click(hwnd, x, y)
            n += 1
        except Exception:
            logger.exception("resume_drive: wheel click failed x=%s y=%s", x, y)
    return n


def _reset_car_main_thread(controls, worker_id=0):
    global _pending_resume_after_reset, _resume_attempts
    import ac

    try:
        ac.ext_resetCar()
    except AttributeError:
        logger.warning("drive_state: ext_resetCar unavailable (CSP disabled)")

    # Repositionnement ALEATOIRE sur le circuit apres chaque episode (30 s).
    # Le worker decouvre ainsi tout le trace, pas seulement son sixieme.
    try:
        _spawn_main_thread(_random_spawn_fraction())
    except Exception:
        logger.exception("drive_state: respawn after reset failed")

    if controls:
        controls.set_controls(0, -1, -1)
    _pending_resume_after_reset = True
    _resume_attempts = 0
    logger.info("drive_state: reset_car on main thread, resume queued")
    _resume_drive_main_thread(controls)


def _repatch_controls_joy_index() -> None:
    """Re-write the SHARED controls.ini with THIS worker's JOY index right before
    the game binds the joystick. The launcher patches once per worker before launch,
    but all game instances share one controls.ini (Windows Documents is hardcoded,
    not per-process), so the LAST worker's patch wins and every game reads the same
    device. Re-patching here, inside the game at the moment it matters, re-asserts
    this instance's own index."""
    try:
        import io
        import os
        import re

        joy_raw = os.environ.get("ACGYM_JOY_INDEX")
        if not joy_raw:
            return
        joy_index = int(joy_raw)
        home = os.path.expanduser("~")
        candidates = []
        env_docs = os.environ.get("AC_DOCUMENTS_PATH")
        if env_docs:
            candidates.append(env_docs)
        candidates.append(os.path.join(home, "Documents", "Assetto Corsa"))
        onedrive = os.environ.get("OneDrive")
        if onedrive:
            candidates.append(os.path.join(onedrive, "Documents", "Assetto Corsa"))
        candidates.append(os.path.join(home, "OneDrive", "Documents", "Assetto Corsa"))
        for docs in candidates:
            controls_path = os.path.join(docs, "cfg", "controls.ini")
            if not os.path.isfile(controls_path):
                continue
            with io.open(controls_path, "r", encoding="utf-8") as fh:
                text = fh.read()
            # Replace only BOUND JOY lines (JOY=<non-negative>), leave JOY=-1 alone.
            new_text = re.sub(r"(?m)^JOY=\d+", "JOY=" + str(joy_index), text)
            if new_text != text:
                with io.open(controls_path, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
                logger.info("drive_state: re-patched controls.ini JOY=%s", joy_index)
            return
    except Exception:
        logger.exception("drive_state: re-patch controls JOY failed")


def _resume_drive_main_thread(controls):
    global _resume_attempts
    _resume_attempts += 1
    # Re-assert our own JOY index before the game binds the joystick for the drive.
    _repatch_controls_joy_index()
    snap = evaluate_driving_state(controls=controls, in_ac_update=True)
    if snap.get("driving"):
        _clear_pending_resume()
        return
    # Only wheel click — Enter closes the game from the session menu.
    # Escape is also dangerous in multi-instance (shared physics can look paused).
    clicks = _post_drive_wheel_clicks()
    logger.info(
        "drive_state: resume_drive attempt=%s wheel_clicks=%s",
        _resume_attempts,
        clicks,
    )
    snap = evaluate_driving_state(controls=controls, in_ac_update=True)
    if snap.get("driving"):
        _clear_pending_resume()


def _clear_pending_resume():
    global _pending_resume_after_reset
    _pending_resume_after_reset = False


def _spawn_fraction_for_worker(worker_id):
    """Point de spawn FIXE du worker : début de son sixième du circuit."""
    slot = int(worker_id) % 6
    return float(slot) / 6.0


def _random_spawn_fraction():
    """Fraction ALEATOIRE le long du circuit (repositionnement apres un episode)."""
    import random

    return random.uniform(0.02, 0.96)


def _spawn_main_thread(fraction):
    """Teleport once along the track spline, after the session physics exist."""
    import ac

    try:
        length = float(ac.ext_getSplineLength(0))
    except Exception:
        logger.exception("drive_state: spline length unavailable")
        return False
    if length <= 1.0:
        logger.warning("drive_state: spline length too small (%s)", length)
        return False
    fraction = min(max(float(fraction), 0.0), 0.98)
    # CSP expects a 0–1 position on the spline, not a distance in meters.
    # A distance in meters clamps every car to the same point, so they block each other.
    look_ahead = min(0.98, fraction + (12.0 / length if length > 1.0 else 0.002))
    try:
        raw = ac.ext_splineToWorld(0, fraction)
        ahead = ac.ext_splineToWorld(0, look_ahead)
    except Exception:
        logger.exception("drive_state: splineToWorld failed")
        return False
    pose = spawn_pose(raw, ahead)
    if pose is None:
        logger.warning("drive_state: spawn ignored, spline not ready fraction=%.3f", fraction)
        return False
    pos, direction = pose
    # CSP's physics.setCarPosition orients the car OPPOSITE to the vector it is
    # given (observed: passing the forward spline direction leaves the car facing
    # backwards on the track). Negate it so the car ends up facing forward.
    direction = (-direction[0], -direction[1], -direction[2])
    moved = False
    try:
        import physics

        physics.setCarVelocity(0, (0.0, 0.0, 0.0))
        physics.setCarPosition(0, pos, direction)
        moved = True
    except Exception:
        logger.exception("drive_state: physics.setCarPosition unavailable")
    if not moved:
        try:
            stamp = time.time()
            ac.ext_storeLua("acai.spawn_id", float(stamp))
            ac.ext_storeLua("acai.spawn_x", pos[0])
            ac.ext_storeLua("acai.spawn_y", pos[1])
            ac.ext_storeLua("acai.spawn_z", pos[2])
            ac.ext_storeLua("acai.dir_x", direction[0])
            ac.ext_storeLua("acai.dir_y", direction[1])
            ac.ext_storeLua("acai.dir_z", direction[2])
            moved = True
        except Exception:
            logger.exception("drive_state: storeLua spawn failed")
    logger.info("drive_state: spawn fraction=%.3f physics=%s", fraction, moved)
    try:
        from bootstrap_log import write
        write("spawn worker fraction={0:.3f} physics={1} pos={2}".format(fraction, moved, pos))
    except Exception:
        pass
    return moved


def _maybe_spawn_once(worker_id, snap):
    global _spawn_once_done, _spawn_driving_since
    if _spawn_once_done or not snap.get("driving"):
        return
    now = time.time()
    if _spawn_driving_since <= 0.0:
        _spawn_driving_since = now
        return
    if now - _spawn_driving_since < SPAWN_DELAY_SEC:
        return
    if _spawn_main_thread(_spawn_fraction_for_worker(worker_id)):
        _spawn_once_done = True
    else:
        _spawn_driving_since = now


def tick_drive_state(controls, deltaT, worker_id=0):
    global _pending_resume_after_reset, _last_auto_resume_wall
    cmds = []
    with _lock:
        if _main_queue:
            cmds = list(_main_queue)
            _main_queue[:] = []

    for cmd in cmds:
        if cmd == "reset":
            _reset_car_main_thread(controls, worker_id)
        elif cmd == "resume":
            # Wheel-icon PostMessage from THIS acs.exe (Enter alone does not leave
            # the Drive menu). External mouse clicks are a backup only.
            _resume_drive_main_thread(controls)
        elif isinstance(cmd, tuple) and cmd and cmd[0] == "spawn":
            _spawn_main_thread(cmd[1])

    snap = evaluate_driving_state(controls=controls, in_ac_update=True)
    with _lock:
        global _cached_state
        _cached_state = dict(snap)

    if snap.get("driving"):
        if _pending_resume_after_reset:
            logger.info("drive_state: driving confirmed after reset")
        _clear_pending_resume()
        _maybe_spawn_once(worker_id, snap)
    elif _pending_resume_after_reset and snap.get("menu_brake_hold"):
        # Only auto-resume when still in the session menu (brake pinned).
        # Once in-game, stop clicking — the launcher handles Drive activation.
        now = time.time()
        if (
            _resume_attempts < MAX_AUTO_RESUME_ATTEMPTS
            and now - _last_auto_resume_wall >= AUTO_RESUME_INTERVAL_SEC
        ):
            _last_auto_resume_wall = now
            _resume_drive_main_thread(controls)
    elif _pending_resume_after_reset and not snap.get("menu_brake_hold"):
        # In-game but not yet "driving" (transient state) — stop clicking.
        _clear_pending_resume()
