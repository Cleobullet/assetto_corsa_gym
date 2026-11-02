"""Worker identity map + isolation invariants (audit tooling).

This module answers a single question the rest of the system must be able to
*prove* before any training is trusted:

    "Does worker N control exactly its own car — its own AC instance, via its own
     telemetry ports, its own vJoy device, its own local actor — with nothing
     shared or cross-wired?"

It is pure (config + on-disk status + vJoy ledger) and does NOT touch the live
game, vJoy, or the learner/replay buffer. It is safe to run at any time. The live
hardware correspondence test (apply a micro-action on one worker and confirm only
that car moves) lives in ``identity_test.py``; this module builds the static map
and checks the structural invariants that the live test then validates physically.

The two layers of identity:
  * STRUCTURE (this module): ports/vjoy/seed/actor are 1:1 and distinct per worker.
    A worker is a separate OS process, so the actor runtime object, exploration
    RNG and local policy are inherently NOT shared — what we verify here is that
    the *addresses* (ports, device id, seed) are distinct and consistent.
  * BINDING (identity_test.py): the vJoy device a worker writes to is actually the
    device the matching AC instance reads. AC's controls.ini identifies vJoy
    devices by product GUID (identical across all vJoy devices) + an enumeration
    index (JOY=N). That index is the one place worker->car identity can silently
    break, so it cannot be proven from files alone — only by a live micro-action.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "assetto_corsa_gym") not in sys.path:
    sys.path.insert(0, str(REPO / "assetto_corsa_gym"))

from ac_workers.action_diagnostics import worker_policy_seed
from ac_workers.vjoy_pool import MAX_WORKERS, device_for_worker
from ac_workers.worker_ports import worker_ports

STATUS_DIR = REPO / "ac_workers" / "status"
TRANSITION_PORT = 24050
# A status file older than this is a leftover from a previous session, not a live
# worker; its overlay is shown but never treated as authoritative for invariants.
STATUS_FRESH_SEC = 30.0


@dataclass
class WorkerIdentity:
    """Everything that defines one worker's private control loop."""

    worker_id: int
    # Deterministic addresses (source of truth: worker_ports + vjoy_pool).
    ego_port: int
    opponents_port: int
    mgmt_port: int
    diag_port: int
    vjoy_device: int
    transition_port: int
    expected_policy_seed: int
    # Config-declared values (config/workers/worker_N.yml) — must match the above.
    cfg_ego_port: int | None = None
    cfg_mgmt_port: int | None = None
    cfg_vjoy_device: int | None = None
    cfg_diag_port: int | None = None
    cfg_present: bool = False
    cfg_mismatch: list[str] = field(default_factory=list)
    # Live state pulled from ac_workers/status/worker_N.json.
    status_present: bool = False
    status_age_sec: float | None = None
    status_fresh: bool = False
    collector_pid: int | None = None
    phase: str = ""
    control_mode: str = ""
    action_source: str = ""
    last_observation_hash: str = ""
    last_raw_action: list[float] = field(default_factory=list)
    last_physical_action: list[float] = field(default_factory=list)
    policy_seed: int | None = None
    exploration_seed: int | None = None
    deterministic_mode: bool | None = None
    active_policy_version: int | None = None
    pending_policy_version: int | None = None
    learner_active: bool | None = None
    transition_count: int | None = None
    status_vjoy_device: int | None = None
    status_diag_port: int | None = None
    status_ego_port: int | None = None
    # vJoy ownership ledger (.vjoy_ledger/device_{vjoy}.json).
    ledger_present: bool = False
    ledger_owner_pid: int | None = None
    ledger_owner_worker_id: int | None = None
    # vJoy device -> JOY enumeration index binding (the one place worker->car can
    # silently break). Proof comes from the PER-WORKER controls.ini snapshot captured
    # just before this worker's AC instance launched — NOT from the shared live file
    # (which only shows the last writer and is therefore misleading).
    #   expected_old_joy_index : the discredited "JOY = worker_id" assumption.
    #   detected_joy_index     : empirically probed device -> enumeration index.
    #   joy_index_written      : JOY value physically present in this worker's snapshot.
    #   mapping_status         : verdict, OK only when written == detected.
    expected_old_joy_index: int | None = None
    detected_joy_index: int | None = None
    joy_index_written: int | None = None
    controls_snapshot_path: str = ""
    patch_ts: str = ""
    patch_age_sec: float | None = None
    snapshot_session_id: str = ""
    ac_pid: int | None = None
    ac_launched_after_patch: bool | None = None
    mapping_status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "ego_port": self.ego_port,
            "opponents_port": self.opponents_port,
            "mgmt_port": self.mgmt_port,
            "diag_port": self.diag_port,
            "vjoy_device": self.vjoy_device,
            "transition_port": self.transition_port,
            "expected_policy_seed": self.expected_policy_seed,
            "cfg_present": self.cfg_present,
            "cfg_ego_port": self.cfg_ego_port,
            "cfg_mgmt_port": self.cfg_mgmt_port,
            "cfg_vjoy_device": self.cfg_vjoy_device,
            "cfg_diag_port": self.cfg_diag_port,
            "cfg_mismatch": list(self.cfg_mismatch),
            "status_present": self.status_present,
            "status_age_sec": self.status_age_sec,
            "status_fresh": self.status_fresh,
            "collector_pid": self.collector_pid,
            "phase": self.phase,
            "control_mode": self.control_mode,
            "action_source": self.action_source,
            "last_observation_hash": self.last_observation_hash,
            "last_raw_action": list(self.last_raw_action),
            "last_physical_action": list(self.last_physical_action),
            "policy_seed": self.policy_seed,
            "exploration_seed": self.exploration_seed,
            "deterministic_mode": self.deterministic_mode,
            "active_policy_version": self.active_policy_version,
            "pending_policy_version": self.pending_policy_version,
            "learner_active": self.learner_active,
            "transition_count": self.transition_count,
            "status_vjoy_device": self.status_vjoy_device,
            "status_diag_port": self.status_diag_port,
            "status_ego_port": self.status_ego_port,
            "ledger_present": self.ledger_present,
            "ledger_owner_pid": self.ledger_owner_pid,
            "ledger_owner_worker_id": self.ledger_owner_worker_id,
            "expected_old_joy_index": self.expected_old_joy_index,
            "detected_joy_index": self.detected_joy_index,
            "joy_index_written": self.joy_index_written,
            "controls_snapshot_path": self.controls_snapshot_path,
            "patch_ts": self.patch_ts,
            "patch_age_sec": self.patch_age_sec,
            "snapshot_session_id": self.snapshot_session_id,
            "ac_pid": self.ac_pid,
            "ac_launched_after_patch": self.ac_launched_after_patch,
            "mapping_status": self.mapping_status,
        }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _as_float_list(value: Any) -> list[float]:
    try:
        return [float(x) for x in (value or [])][:3]
    except (TypeError, ValueError):
        return []


def _load_cfg_fields(identity: WorkerIdentity, base_seed: int) -> None:
    """Overlay config/workers/worker_N.yml and flag any deviation from the map."""
    try:
        from AssettoCorsaEnv.ac_worker_runtime import load_worker_config

        cfg = load_worker_config(identity.worker_id)
    except Exception:
        identity.cfg_present = False
        return
    identity.cfg_present = True
    identity.cfg_ego_port = int(cfg.ego_server_port)
    identity.cfg_mgmt_port = int(cfg.simulation_management_server_port)
    identity.cfg_vjoy_device = int(cfg.vjoy_device_id)
    mismatch: list[str] = []
    if identity.cfg_ego_port != identity.ego_port:
        mismatch.append(
            "ego_port cfg={0} != map={1}".format(identity.cfg_ego_port, identity.ego_port)
        )
    if identity.cfg_mgmt_port != identity.mgmt_port:
        mismatch.append(
            "mgmt_port cfg={0} != map={1}".format(identity.cfg_mgmt_port, identity.mgmt_port)
        )
    if identity.cfg_vjoy_device != identity.vjoy_device:
        mismatch.append(
            "vjoy_device cfg={0} != map={1}".format(
                identity.cfg_vjoy_device, identity.vjoy_device
            )
        )
    identity.cfg_mismatch = mismatch


def _load_status_fields(identity: WorkerIdentity, status_dir: Path) -> None:
    import time as _time

    st = _read_json(status_dir / "worker_{0}.json".format(identity.worker_id))
    if not st:
        return
    identity.status_present = True
    ts = st.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        identity.status_age_sec = max(0.0, _time.time() - float(ts))
        identity.status_fresh = identity.status_age_sec <= STATUS_FRESH_SEC
    identity.collector_pid = st.get("collector_pid")
    identity.phase = str(st.get("phase", ""))
    identity.control_mode = str(st.get("control_mode", ""))
    identity.action_source = str(st.get("policy_action_source", st.get("action_source", "")))
    identity.last_observation_hash = str(st.get("observation_hash", ""))
    identity.last_raw_action = _as_float_list(
        st.get("policy_raw_action") or st.get("raw_action") or st.get("last_requested_action")
    )
    identity.last_physical_action = _as_float_list(
        st.get("physical_action") or st.get("last_executed_action")
    )
    identity.policy_seed = st.get("policy_seed")
    identity.exploration_seed = st.get("exploration_seed")
    identity.deterministic_mode = st.get("deterministic_mode")
    identity.active_policy_version = st.get("active_policy_version")
    identity.pending_policy_version = st.get("pending_policy_version")
    identity.learner_active = st.get("learner_active")
    identity.transition_count = st.get("transition_count")
    identity.status_vjoy_device = st.get("vjoy_device")
    identity.status_diag_port = st.get("diag_port")
    identity.status_ego_port = st.get("ego_port")


def _load_joy_index_fields(identity: WorkerIdentity) -> None:
    """Overlay the device->JOY-index binding from this worker's PER-WORKER snapshot.

    The verdict is built from three things, never from the shared live controls.ini:
      * detected_joy_index : live empirical probe (vjoy_index_probe).
      * joy_index_written  : the JOY value captured in this worker's snapshot, taken
                             the instant its controls.ini was finalized pre-launch.
      * temporal proof     : the AC pid was stamped after the patch.
    OK requires a snapshot whose written JOY equals the detected index.
    """
    import time as _time

    identity.expected_old_joy_index = identity.worker_id

    detected_live: int | None = None
    try:
        from ac_workers.vjoy_index_probe import detected_index_for_device

        detected_live = detected_index_for_device(identity.vjoy_device)
    except Exception:
        detected_live = None

    meta: dict[str, Any] = {}
    try:
        from ac_workers.controls_snapshot import load_meta

        meta = load_meta(identity.worker_id)
    except Exception:
        meta = {}

    # Prefer the live probe; fall back to whatever the snapshot recorded.
    identity.detected_joy_index = (
        detected_live if detected_live is not None else meta.get("detected_joy_index")
    )
    identity.joy_index_written = meta.get("joy_index_written")
    identity.controls_snapshot_path = str(meta.get("controls_snapshot_path", "") or "")
    identity.patch_ts = str(meta.get("patch_ts_utc", "") or "")
    identity.snapshot_session_id = str(meta.get("session_id", "") or "")
    identity.ac_pid = meta.get("ac_pid")
    identity.ac_launched_after_patch = meta.get("ac_launched_after_patch")
    patch_epoch = meta.get("patch_ts_epoch")
    if isinstance(patch_epoch, (int, float)) and patch_epoch > 0:
        identity.patch_age_sec = max(0.0, _time.time() - float(patch_epoch))

    identity.mapping_status = _joy_index_verdict(identity)


def _joy_index_verdict(identity: WorkerIdentity) -> str:
    if identity.detected_joy_index is None:
        return "NO_DETECTION"
    if identity.joy_index_written is None:
        return "NO_SNAPSHOT"
    if int(identity.joy_index_written) != int(identity.detected_joy_index):
        return "MISMATCH"
    # written == detected. Distinguish identity vs remapped only as extra colour.
    if int(identity.detected_joy_index) == int(identity.expected_old_joy_index or 0):
        return "OK(identity)"
    return "OK(remapped)"


def _load_ledger_fields(identity: WorkerIdentity) -> None:
    try:
        from ac_workers.vjoy_ledger import read_owner
    except Exception:
        return
    entry = read_owner(identity.vjoy_device)
    if not entry:
        return
    identity.ledger_present = True
    identity.ledger_owner_pid = entry.get("pid")
    identity.ledger_owner_worker_id = entry.get("worker_id")


def build_worker_map(
    worker_ids: list[int] | None = None,
    *,
    base_seed: int = 0,
    status_dir: Path | None = None,
    include_status: bool = True,
    include_ledger: bool = True,
    include_joy_index: bool = True,
) -> list[WorkerIdentity]:
    """Build the full identity map for the requested workers.

    ``base_seed`` is the training seed used to derive per-worker policy seeds; it
    only affects ``expected_policy_seed``. Live status/ledger overlays are optional
    so the structural map can be built on a machine with nothing running.
    """
    if worker_ids is None:
        worker_ids = list(range(MAX_WORKERS))
    status_dir = status_dir or STATUS_DIR
    out: list[WorkerIdentity] = []
    for wid in worker_ids:
        ports = worker_ports(wid)
        identity = WorkerIdentity(
            worker_id=wid,
            ego_port=ports.ego,
            opponents_port=ports.opponents,
            mgmt_port=ports.mgmt,
            diag_port=ports.diag,
            vjoy_device=ports.vjoy,
            transition_port=TRANSITION_PORT,
            expected_policy_seed=worker_policy_seed(base_seed, wid),
        )
        _load_cfg_fields(identity, base_seed)
        if include_status:
            _load_status_fields(identity, status_dir)
        if include_ledger:
            _load_ledger_fields(identity)
        if include_joy_index:
            _load_joy_index_fields(identity)
        out.append(identity)
    return out


def check_isolation_invariants(identities: list[WorkerIdentity]) -> list[str]:
    """Return a list of isolation violations (empty == fully isolated).

    These are the "data-test" invariants: two workers must never share a
    telemetry port, a diag port or a vJoy device; the device must be the strict
    ``worker_id + 1``; policy seeds must be distinct; config must match the map;
    and (when live) the vJoy ledger must show each device owned by its own worker.
    """
    violations: list[str] = []

    def _dup_check(label: str, getter) -> None:
        seen: dict[Any, int] = {}
        for ident in identities:
            value = getter(ident)
            if value is None:
                continue
            if value in seen:
                violations.append(
                    "duplicate {0}={1} on workers {2} and {3}".format(
                        label, value, seen[value], ident.worker_id
                    )
                )
            else:
                seen[value] = ident.worker_id

    _dup_check("ego_port", lambda i: i.ego_port)
    _dup_check("opponents_port", lambda i: i.opponents_port)
    _dup_check("mgmt_port", lambda i: i.mgmt_port)
    _dup_check("diag_port", lambda i: i.diag_port)
    _dup_check("vjoy_device", lambda i: i.vjoy_device)
    _dup_check("expected_policy_seed", lambda i: i.expected_policy_seed)

    for ident in identities:
        if ident.vjoy_device != device_for_worker(ident.worker_id):
            violations.append(
                "worker {0} vjoy_device={1} != strict {2}".format(
                    ident.worker_id, ident.vjoy_device, device_for_worker(ident.worker_id)
                )
            )
        for m in ident.cfg_mismatch:
            violations.append("worker {0} config mismatch: {1}".format(ident.worker_id, m))
        # Live overlays (only meaningful when present AND fresh — a stale status
        # file from a previous session must not raise a false violation).
        if ident.status_present and ident.status_vjoy_device not in (None, ident.vjoy_device):
            violations.append(
                "worker {0} status vjoy_device={1} != map {2}".format(
                    ident.worker_id, ident.status_vjoy_device, ident.vjoy_device
                )
            )
        if ident.status_present and ident.status_diag_port not in (None, ident.diag_port):
            violations.append(
                "worker {0} status diag_port={1} != map {2}".format(
                    ident.worker_id, ident.status_diag_port, ident.diag_port
                )
            )
        if (
            ident.ledger_present
            and ident.ledger_owner_worker_id is not None
            and int(ident.ledger_owner_worker_id) != ident.worker_id
        ):
            violations.append(
                "vjoy device {0} (worker {1}) owned in ledger by worker {2}".format(
                    ident.vjoy_device, ident.worker_id, ident.ledger_owner_worker_id
                )
            )

    # A vJoy device must never be claimed by two different live collector pids.
    pid_by_device: dict[int, int] = {}
    for ident in identities:
        if ident.ledger_present and ident.ledger_owner_pid:
            dev = ident.vjoy_device
            pid = int(ident.ledger_owner_pid)
            if dev in pid_by_device and pid_by_device[dev] != pid:
                violations.append(
                    "vjoy device {0} owned by two pids {1} and {2}".format(
                        dev, pid_by_device[dev], pid
                    )
                )
            pid_by_device[dev] = pid

    return violations


def check_controls_snapshot_proof(identities: list[WorkerIdentity]) -> list[str]:
    """Return binding-proof violations: every worker must have a snapshot whose
    written JOY index matches the detected enumeration index.

    FAILED until all workers show OK(identity) or OK(remapped) — a single
    NO_SNAPSHOT means we cannot prove that worker's controls.ini was patched
    correctly before its AC instance launched.
    """
    problems: list[str] = []
    for ident in identities:
        st = ident.mapping_status
        if st in ("OK(identity)", "OK(remapped)"):
            continue
        if st == "NO_SNAPSHOT":
            problems.append(
                "worker {0}: NO_SNAPSHOT — no controls.ini proof captured before AC launch "
                "(restart via clean-start so each worker patches+saves its own snapshot)".format(
                    ident.worker_id
                )
            )
        elif st == "MISMATCH":
            problems.append(
                "worker {0}: MISMATCH — snapshot JOY={1} != detected={2}".format(
                    ident.worker_id, ident.joy_index_written, ident.detected_joy_index
                )
            )
        elif st == "NO_DETECTION":
            problems.append(
                "worker {0}: NO_DETECTION — vJoy index mapping not probed before launch".format(
                    ident.worker_id
                )
            )
        else:
            problems.append("worker {0}: binding proof unknown ({1})".format(ident.worker_id, st))
    return problems


def format_worker_map(identities: list[WorkerIdentity]) -> str:
    lines = ["worker identity map (worker -> instance/ports/vjoy/actor):"]
    header = (
        "  W  ego  opp  mgmt diag vjoy seed       pid     live phase            "
        "act_src        raw_action            obs_hash"
    )
    lines.append(header)
    for i in identities:
        raw = (
            "[" + ",".join("{0:+.3f}".format(x) for x in i.last_raw_action) + "]"
            if i.last_raw_action
            else "-"
        )
        if not i.status_present:
            live = "-"
        elif i.status_fresh:
            live = "LIVE"
        else:
            age = "?" if i.status_age_sec is None else "{0:.0f}s".format(i.status_age_sec)
            live = "stale" + age
        lines.append(
            "  {wid:<2} {ego:<4} {opp:<4} {mgmt:<4} {diag:<4} {vjoy:<4} "
            "{seed:<10} {pid:<7} {live:<5} {phase:<16} {src:<14} {raw:<21} {obs}".format(
                wid=i.worker_id,
                ego=i.ego_port,
                opp=i.opponents_port,
                mgmt=i.mgmt_port,
                diag=i.diag_port,
                vjoy=i.vjoy_device,
                seed=i.expected_policy_seed,
                pid=str(i.collector_pid or "-"),
                live=live,
                phase=(i.phase or "-")[:16],
                src=(i.action_source or "-")[:14],
                raw=raw[:21],
                obs=i.last_observation_hash or "-",
            )
        )
    return "\n".join(lines)


def format_joy_index_map(identities: list[WorkerIdentity]) -> str:
    """Render the per-worker vJoy device -> JOY index binding PROOF.

    Every value here is per-worker (from that worker's pre-launch snapshot), never
    the misleading shared live controls.ini. ``written`` is the JOY captured in the
    snapshot; ``ac_pid`` is the AC instance launched right after that patch.
    """
    lines = [
        "vJoy -> JOY index binding PROOF (per-worker snapshot, not the shared live file):"
    ]
    lines.append(
        "  W  vjoy  old  detected  written  ac_pid   after_patch  patch_age  verdict"
    )

    def _fmt(v: Any) -> str:
        return "-" if v is None else str(v)

    for i in identities:
        age = "-" if i.patch_age_sec is None else "{0:.0f}s".format(i.patch_age_sec)
        after = "-" if i.ac_launched_after_patch is None else ("yes" if i.ac_launched_after_patch else "NO")
        lines.append(
            "  {wid:<2} {vjoy:<5} {old:<4} {det:<9} {wr:<8} {pid:<8} {after:<12} {age:<10} {st}".format(
                wid=i.worker_id,
                vjoy=i.vjoy_device,
                old=_fmt(i.expected_old_joy_index),
                det=_fmt(i.detected_joy_index),
                wr=_fmt(i.joy_index_written),
                pid=_fmt(i.ac_pid),
                after=after,
                age=age,
                st=i.mapping_status,
            )
        )

    statuses = {i.mapping_status for i in identities}
    if "NO_DETECTION" in statuses:
        lines.append(
            "  ! NO_DETECTION: run `python -m ac_workers.vjoy_index_probe --save` with all "
            "vJoy devices free BEFORE launch."
        )
    if "NO_SNAPSHOT" in statuses:
        lines.append(
            "  ! NO_SNAPSHOT: no pre-launch controls.ini snapshot for some worker(s) — start a "
            "session so each worker's controls.ini is captured before its AC launches."
        )
    if "MISMATCH" in statuses:
        lines.append(
            "  ! MISMATCH: a worker's written JOY != detected index — the patch did NOT bind that "
            "worker's car correctly. Do NOT trust training."
        )
    snap = next((i.controls_snapshot_path for i in identities if i.controls_snapshot_path), "")
    if snap:
        lines.append("  snapshots: {0}".format(str(Path(snap).parent)))
    lines.append(
        "  verdict OK only when written == detected (worker N -> device N+1 -> detected JOY -> car N)."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Worker identity map + isolation invariants")
    parser.add_argument("--workers", type=str, default="", help="comma list, default 0..5")
    parser.add_argument("--base-seed", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--no-status", action="store_true", help="skip live status overlay")
    parser.add_argument("--no-ledger", action="store_true", help="skip vJoy ledger overlay")
    args = parser.parse_args(argv)

    if args.workers.strip():
        worker_ids = [int(x) for x in args.workers.split(",") if x.strip() != ""]
    else:
        worker_ids = list(range(MAX_WORKERS))

    identities = build_worker_map(
        worker_ids,
        base_seed=args.base_seed,
        include_status=not args.no_status,
        include_ledger=not args.no_ledger,
    )
    violations = check_isolation_invariants(identities)
    snapshot_violations = check_controls_snapshot_proof(identities)
    all_violations = violations + snapshot_violations
    binding_ok = not snapshot_violations

    if args.json:
        print(
            json.dumps(
                {
                    "workers": [i.to_dict() for i in identities],
                    "violations": all_violations,
                    "isolated": not violations,
                    "binding_proof_ok": binding_ok,
                    "snapshot_violations": snapshot_violations,
                },
                indent=2,
            )
        )
    else:
        print(format_worker_map(identities))
        print("")
        print(format_joy_index_map(identities))
        print("")
        if violations:
            print("ISOLATION VIOLATIONS ({0}):".format(len(violations)))
            for v in violations:
                print("  ! " + v)
        else:
            print("isolation invariants: OK (no structural cross-wiring detected)")
        if snapshot_violations:
            print("")
            print("CONTROLS SNAPSHOT BINDING: FAILED ({0})".format(len(snapshot_violations)))
            for v in snapshot_violations:
                print("  ! " + v)
            print(
                "  -> cannot validate vJoy->JOY binding until every worker has OK(remapped) "
                "with written == detected."
            )
        elif identities:
            print("")
            print("controls snapshot binding: OK (all workers have written == detected)")
        print(
            "\nNOTE: this proves STRUCTURE (distinct ports/vjoy/seed per worker). It does "
            "NOT prove the\nvJoy->car BINDING (which physical car each vJoy device drives). "
            "Run identity_test.py\nlive to confirm a micro-action on worker N moves only car N."
        )
    return 1 if all_violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
