"""Offline tests for named C4D training sessions."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_live(work: Path, *, steps: int = 100) -> None:
    _write(work / "model" / "checkpoints" / "resume" / "policy_net.pth")
    _write(
        work / "model" / "checkpoints" / "resume" / "policy_metadata.json",
        json.dumps({"training_step": steps}),
    )
    _write(work / "model" / "latest.pt")
    _write(work / "model" / "latest.json", json.dumps({"policy_version": steps, "version_dir": "version_x"}))
    _write(work / "model" / "publish" / "policy_net.pth")
    _write(
        work / "trainer_state.json",
        json.dumps({"steps": steps, "updates": 80, "replay_size": 50}),
    )


def test_save_load_new_delete(tmp_path: Path) -> None:
    from ac_workers import training_sessions as ts

    work = tmp_path / "live"
    root = tmp_path / "sessions"
    _seed_live(work, steps=123)
    meta = ts.save_session("droite-bug", work_dir=work, root=root)
    assert meta["name"] == "droite-bug"
    assert meta["steps"] == 123
    assert (root / "droite-bug" / "model" / "checkpoints" / "resume" / "policy_net.pth").is_file()
    assert ts.current_session_name(root=root) == "droite-bug"

    created = ts.new_session("gauche-v1", work_dir=work, root=root, seed_fresh_policy=False)
    assert created["name"] == "gauche-v1"
    assert created["autosaved"].startswith("autosave_")
    assert not ts.has_live_weights(work)
    assert not (work / "model" / "publish_versions").exists()
    assert ts.current_session_name(root=root) == "gauche-v1"

    loaded = ts.load_session("droite-bug", work_dir=work, root=root)
    assert loaded["steps"] == 123
    assert ts.has_live_weights(work)
    assert ts.current_session_name(root=root) == "droite-bug"

    try:
        ts.delete_session("droite-bug", root=root)
        raise AssertionError("should refuse deleting current")
    except ValueError:
        pass
    ts.load_session("gauche-v1", work_dir=work, root=root)
    ts.delete_session("droite-bug", root=root)
    names = [s["name"] for s in ts.list_sessions(root=root)]
    assert "droite-bug" not in names
    assert "gauche-v1" in names


def test_wipe_live_weights_removes_resume(tmp_path: Path) -> None:
    from ac_workers.training_sessions import wipe_live_weights

    work = tmp_path / "live"
    _seed_live(work, steps=56000)
    assert (work / "model" / "checkpoints" / "resume" / "policy_net.pth").is_file()
    wipe_live_weights(work)
    assert not (work / "model" / "checkpoints" / "resume" / "policy_net.pth").exists()
    assert not (work / "model" / "latest.json").exists()
    assert not (work / "trainer_state.json").exists()


def test_reject_bad_name() -> None:
    from ac_workers.training_sessions import sanitize_session_name

    try:
        sanitize_session_name("../x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert sanitize_session_name("  Monza_v2 ") == "Monza_v2"


def test_control_center_train_commands(tmp_path: Path) -> None:
    from unittest import mock

    from ac_workers.control_center import ControlCenter

    with mock.patch("ac_workers.session_cleanup.cleanup_stale_state"):
        center = ControlCenter(REPO)
    center._learner_is_live_process = lambda: False  # type: ignore[method-assign]
    help_txt = center.handle_command("help") or ""
    assert "train list" in help_txt
    assert "train" in help_txt and "wipe" in help_txt
    usage = center.handle_training_session_command("nope")
    assert "train wipe" in usage
    with mock.patch(
        "ac_workers.training_sessions.wipe_and_reseed",
        return_value={
            "published_pointer": {"policy_version": 0},
            "resume_checkpoint_present": False,
        },
    ):
        wiped = center.handle_command("train wipe") or ""
        memory_cmd = center.handle_command("memory clear") or ""
    assert "memoire effacee" in wiped
    assert "version=0" in wiped
    assert "resume=False" in wiped
    assert "memoire effacee" in memory_cmd


def main() -> int:
    import tempfile

    test_reject_bad_name()
    print("OK test_reject_bad_name")
    with tempfile.TemporaryDirectory() as tmp:
        test_save_load_new_delete(Path(tmp))
        print("OK test_save_load_new_delete")
        test_wipe_live_weights_removes_resume(Path(tmp))
        print("OK test_wipe_live_weights_removes_resume")
        test_control_center_train_commands(Path(tmp))
        print("OK test_control_center_train_commands")
    print("Test-TrainingSessions-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
