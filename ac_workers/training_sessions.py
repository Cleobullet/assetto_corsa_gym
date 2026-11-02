"""Named training snapshots for the C4D Control Center.

Live weights stay in outputs/multi_sac_cpu_v3 (collectors and the learner
already use that folder). Named copies live in outputs/training_sessions/<name>/.
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
LIVE_WORK_DIR = REPO / "outputs" / "multi_sac_cpu_v3"
NEURO_DIR = REPO / "outputs" / "neuroevolution"
SESSIONS_ROOT = REPO / "outputs" / "training_sessions"
CURRENT_PATH = SESSIONS_ROOT / "current.json"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

_COPY_DIRS = (
    "model/checkpoints/resume",
    "model/publish",
    "model/publish_versions",
)
_COPY_FILES = (
    "model/latest.pt",
    "model/latest.json",
    "trainer_state.json",
    "training_environment.json",
)
_WIPE_PATHS = _COPY_DIRS + _COPY_FILES + ("model/checkpoints",)


def sanitize_session_name(raw: str) -> str:
    name = (raw or "").strip().replace(" ", "-")
    if not NAME_RE.match(name):
        raise ValueError(
            "nom invalide (lettres, chiffres, . _ - ; 1-63 caracteres, commencer par une lettre ou un chiffre)"
        )
    if name.lower() in {
        "current", "index", "new", "list", "save", "load", "delete",
        "wipe", "forget", "memory", "clear",
    }:
        raise ValueError("nom reserve: {0}".format(name))
    return name


def session_dir(name: str, *, root: Path = SESSIONS_ROOT) -> Path:
    return root / sanitize_session_name(name)


def current_session_name(*, root: Path = SESSIONS_ROOT) -> str:
    path = root / "current.json"
    data = _read_json(path)
    return str(data.get("name") or "").strip()


def set_current_session(name: str, *, root: Path = SESSIONS_ROOT) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": sanitize_session_name(name),
        "updated_at": _now(),
    }
    (root / "current.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def has_live_weights(work_dir: Path = LIVE_WORK_DIR) -> bool:
    """True when a real policy checkpoint exists (not just the 2-byte latest.pt marker)."""
    resume = work_dir / "model" / "checkpoints" / "resume" / "policy_net.pth"
    if resume.is_file() and resume.stat().st_size > 100:
        return True
    publish = work_dir / "model" / "publish" / "policy_net.pth"
    if publish.is_file() and publish.stat().st_size > 100:
        return True
    pointer = _read_json(work_dir / "model" / "latest.json")
    version_name = str(pointer.get("version_dir") or "").strip()
    if version_name:
        version_policy = (
            work_dir / "model" / "publish_versions" / version_name / "policy_net.pth"
        )
        if version_policy.is_file() and version_policy.stat().st_size > 100:
            return True
    versions = work_dir / "model" / "publish_versions"
    if versions.is_dir():
        for path in versions.glob("version_*/policy_net.pth"):
            if path.is_file() and path.stat().st_size > 100:
                return True
    return False


def live_snapshot_meta(work_dir: Path = LIVE_WORK_DIR) -> dict[str, Any]:
    trainer = _read_json(work_dir / "trainer_state.json")
    learner = _read_json(work_dir / "learner_status.json")
    resume_meta = _read_json(work_dir / "model" / "checkpoints" / "resume" / "policy_metadata.json")
    pointer = _read_json(work_dir / "model" / "latest.json")
    steps = int(
        trainer.get("steps")
        or learner.get("global_steps")
        or learner.get("learner_global_steps")
        or resume_meta.get("training_step")
        or pointer.get("policy_version")
        or 0
    )
    environment = _read_json(work_dir / "training_environment.json")
    return {
        "car": environment.get("car"),
        "track": environment.get("track"),
        "environment_source": environment.get("source"),
        "steps": steps,
        "updates": int(trainer.get("updates") or learner.get("updates") or 0),
        "replay_size": int(trainer.get("replay_size") or learner.get("replay_size") or 0),
        "has_weights": has_live_weights(work_dir),
    }


def list_sessions(*, root: Path = SESSIONS_ROOT) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    current = current_session_name(root=root)
    out: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        meta = _read_json(path / "session.json")
        name = str(meta.get("name") or path.name)
        item = {
            "car": meta.get("car"),
            "track": meta.get("track"),
            "name": name,
            "current": name == current,
            "steps": int(meta.get("steps") or 0),
            "updates": int(meta.get("updates") or 0),
            "replay_size": int(meta.get("replay_size") or 0),
            "saved_at": str(meta.get("saved_at") or ""),
            "kind": str(meta.get("kind") or "snapshot"),
        }
        out.append(item)
    return out


def save_session(
    name: str,
    *,
    work_dir: Path = LIVE_WORK_DIR,
    root: Path = SESSIONS_ROOT,
    kind: str = "snapshot",
) -> dict[str, Any]:
    slug = sanitize_session_name(name)
    final_dest = root / slug
    root.mkdir(parents=True, exist_ok=True)
    dest = root / (".saving-" + uuid.uuid4().hex)
    dest.mkdir()
    copied: list[str] = []
    for rel in _COPY_DIRS:
        src = work_dir / rel
        if src.is_dir():
            shutil.copytree(src, dest / rel, dirs_exist_ok=True)
            copied.append(rel)
    for rel in _COPY_FILES:
        src = work_dir / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
            copied.append(rel)
    # Neuroévolution : copier aussi l'état courant (population.npz + summary +
    # generation). La population est déjà « dernière génération uniquement »
    # (remplacée atomiquement à chaque save), donc la copie reste légère.
    if NEURO_DIR.is_dir():
        shutil.copytree(NEURO_DIR, dest / "neuroevolution", dirs_exist_ok=True)
        copied.append("neuroevolution")
    meta = live_snapshot_meta(dest)
    payload = {
        "name": slug,
        "kind": kind,
        "saved_at": _now(),
        "copied": copied,
        **meta,
    }
    (dest / "session.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if not session_has_weights(dest):
        raise ValueError("sauvegarde sans modele : copie non publiee")
    if final_dest.exists():
        backup = root / (slug + "-previous-" + uuid.uuid4().hex[:8])
        final_dest.rename(backup)
    dest.rename(final_dest)
    set_current_session(slug, root=root)
    return payload


def session_has_weights(src: Path) -> bool:
    """True when a named session folder contains a usable policy checkpoint."""
    # Neuroévolution : la population (dernière génération) suffit.
    if (src / "neuroevolution" / "population.npz").is_file():
        return True
    resume = src / "model" / "checkpoints" / "resume" / "policy_net.pth"
    if resume.is_file() and resume.stat().st_size > 100:
        return True
    publish = src / "model" / "publish" / "policy_net.pth"
    if publish.is_file() and publish.stat().st_size > 100:
        return True
    versions = src / "model" / "publish_versions"
    if versions.is_dir():
        for path in versions.glob("version_*/policy_net.pth"):
            if path.is_file() and path.stat().st_size > 100:
                return True
    return False


def load_session(
    name: str,
    *,
    work_dir: Path = LIVE_WORK_DIR,
    root: Path = SESSIONS_ROOT,
) -> dict[str, Any]:
    slug = sanitize_session_name(name)
    src = root / slug
    meta = _read_json(src / "session.json")
    if not src.is_dir() or not meta:
        raise FileNotFoundError("session introuvable: {0}".format(slug))
    if not session_has_weights(src):
        raise ValueError(
            "session {0} sans modele (dossier vide). "
            "Utilise « Nouvel entraînement » puis démarre l’apprentissage "
            "sans recliquer sur Charger.".format(slug)
        )
    wipe_live_weights(work_dir)
    for rel in _COPY_DIRS:
        from_path = src / rel
        if from_path.is_dir():
            target = work_dir / rel
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(from_path, target)
    for rel in _COPY_FILES:
        from_path = src / rel
        if from_path.is_file():
            (work_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(from_path, work_dir / rel)
    # Neuroévolution : restaurer la population sauvegardée.
    _nevo_src = src / "neuroevolution"
    if _nevo_src.is_dir():
        if NEURO_DIR.exists():
            shutil.rmtree(NEURO_DIR)
        shutil.copytree(_nevo_src, NEURO_DIR)
    set_current_session(slug, root=root)
    return meta


def new_session(
    name: str,
    *,
    work_dir: Path = LIVE_WORK_DIR,
    root: Path = SESSIONS_ROOT,
    autosave_existing: bool = True,
    seed_fresh_policy: bool = True,
) -> dict[str, Any]:
    slug = sanitize_session_name(name)
    dest = root / slug
    if dest.exists():
        raise FileExistsError("session deja existante: {0}".format(slug))
    autosaved = ""
    if autosave_existing and has_live_weights(work_dir):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        autosaved = "autosave_{0}".format(stamp)
        save_session(autosaved, work_dir=work_dir, root=root, kind="autosave")
    wipe_live_weights(work_dir)
    # Neuroévolution : repartir de zéro (population + résumé).
    shutil.rmtree(NEURO_DIR, ignore_errors=True)
    if seed_fresh_policy:
        from ac_workers.init_fresh_policy import init_fresh_policy

        init_fresh_policy(
            config_path=REPO / "config_first_sac_cpu_v3.yml",
            work_dir=work_dir,
            state_dim=50,
            action_dim=3,
        )
    if not has_live_weights(work_dir):
        raise RuntimeError(
            "train new: modele neuf introuvable apres initialisation (publish_versions)"
        )
    # Persist the fresh weights into the named session so a later "Charger"
    # cannot wipe the live model with an empty folder.
    payload = save_session(slug, work_dir=work_dir, root=root, kind="new")
    payload["autosaved"] = autosaved
    (dest / "session.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    set_current_session(slug, root=root)
    return payload


def delete_session(
    name: str,
    *,
    root: Path = SESSIONS_ROOT,
) -> str:
    slug = sanitize_session_name(name)
    if current_session_name(root=root) == slug:
        raise ValueError("impossible de supprimer la session active ({0})".format(slug))
    dest = root / slug
    if not dest.is_dir():
        raise FileNotFoundError("session introuvable: {0}".format(slug))
    shutil.rmtree(dest)
    return slug


def wipe_live_weights(work_dir: Path = LIVE_WORK_DIR) -> None:
    """Remove live policy files so the next learner start is from scratch."""
    for rel in (
        "model/checkpoints/resume",
        "model/publish",
        "model/publish_versions",
    ):
        path = work_dir / rel
        if path.is_dir():
            shutil.rmtree(path)
    for rel in ("model/latest.pt", "model/latest.json", "trainer_state.json"):
        path = work_dir / rel
        if path.is_file():
            path.unlink()
    ckpt = work_dir / "model" / "checkpoints"
    if ckpt.is_dir():
        for child in ckpt.glob("step_*"):
            if child.is_dir():
                shutil.rmtree(child)
            elif child.is_file():
                child.unlink()
    status = work_dir / "learner_status.json"
    if status.is_file():
        try:
            status.unlink()
        except OSError:
            pass


def wipe_and_reseed(
    work_dir: Path = LIVE_WORK_DIR,
    *,
    config_path: Path | None = None,
    state_dim: int = 50,
    action_dim: int = 3,
) -> dict[str, Any]:
    """Erase live weights/resume/replay pointers and publish a random policy."""
    from ac_workers.init_fresh_policy import init_fresh_policy
    from ac_workers.policy_metadata import read_pointer

    wipe_live_weights(work_dir)
    # PBT : vider aussi le cerveau principal + l'historique des essais, sinon les
    # workers repartent de l'ancien cerveau (incompatible après un changement de
    # récompense ou d'architecture).
    import shutil
    shutil.rmtree(work_dir / "pbt_main", ignore_errors=True)
    shutil.rmtree(work_dir / "pbt_main.tmp", ignore_errors=True)
    for _p in (work_dir / "trials.json",):
        try:
            _p.unlink()
        except OSError:
            pass
    # Neuroévolution : repartir de zéro (population + résumé).
    shutil.rmtree(NEURO_DIR, ignore_errors=True)
    result = init_fresh_policy(
        config_path=config_path or (REPO / "config_first_sac_cpu_v3.yml"),
        work_dir=work_dir,
        state_dim=state_dim,
        action_dim=action_dim,
        created_from="wipe",
    )
    result["resume_checkpoint_present"] = (
        work_dir / "model" / "checkpoints" / "resume" / "policy_net.pth"
    ).is_file()
    result["published_pointer"] = read_pointer(work_dir / "model")
    return result


def format_session_list(*, work_dir: Path = LIVE_WORK_DIR, root: Path = SESSIONS_ROOT) -> str:
    current = current_session_name(root=root) or "-"
    live = live_snapshot_meta(work_dir)
    lines = [
        "session active: {0}".format(current),
        "live: steps={0} updates={1} replay={2} weights={3}".format(
            live["steps"],
            live["updates"],
            live["replay_size"],
            "yes" if live["has_weights"] else "no",
        ),
        "usage: train list | train save <nom> | train load <nom> | train new <nom> | train delete <nom>",
    ]
    sessions = list_sessions(root=root)
    if not sessions:
        lines.append("(aucune session nommee)")
        return "\n".join(lines)
    lines.append("sessions:")
    for item in sessions:
        mark = "*" if item["current"] else " "
        lines.append(
            " {0} {1}  steps={2} updates={3} replay={4}  {5}".format(
                mark,
                item["name"],
                item["steps"],
                item["updates"],
                item["replay_size"],
                item["saved_at"][:19],
            )
        )
    return "\n".join(lines)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
