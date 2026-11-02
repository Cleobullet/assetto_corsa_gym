"""Outil de debug : explique EXACTEMENT pourquoi un worker a été fermé.

Le serveur C4D écrit chaque événement de vie d'un worker (ready / auto-retry /
failed / start_failed) dans `ac_workers/logs/worker_events.log` via
`ControlCenter._log_worker_event`. Cet outil lit ce journal et, pour chaque
fermeture de worker, joint les dernières lignes des logs concernés (stderr du
collecteur, stdout, journal du lanceur, py_log du jeu) afin de comprendre la
cause exacte du crash.

Usage :
    python -m ac_workers.debug_worker_crash             # rapport des kills
    python -m ac_workers.debug_worker_crash --tail 40   # + 40 lignes de logs
    python -m ac_workers.debug_worker_crash --watch     # suit en direct
    python -m ac_workers.debug_worker_crash --clear     # vide le journal puis suit
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGS = os.path.join(_REPO, "ac_workers", "logs")
_EVENTS = os.path.join(_LOGS, "worker_events.log")

_KILL_EVENTS = {"auto-retry", "failed", "start_failed"}


def _latest(paths_glob):
    hits = sorted(glob.glob(paths_glob))
    return hits[-1] if hits else None


def _tail(path, n):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return lines[-n:] if n > 0 else lines
    except Exception:
        return None


def _read_events():
    if not os.path.exists(_EVENTS):
        return []
    out = []
    with open(_EVENTS, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"raw": line})
    return out


def _worker_logs(wid):
    return {
        "stderr": _latest(os.path.join(_LOGS, "worker_{0}_*_stderr.log".format(wid))),
        "stdout": _latest(os.path.join(_LOGS, "worker_{0}_*_stdout.log".format(wid))),
        "launcher": _latest(os.path.join(_LOGS, "worker_{0}_*.log".format(wid))),
        "game_pylog": _latest(
            os.path.join(
                _REPO,
                "ac_profiles",
                "workers",
                "worker_{0}".format(wid),
                "Documents",
                "Assetto Corsa",
                "logs",
                "py_log.txt",
            )
        ),
    }


def _render_event(ev, tail_n):
    if "raw" in ev:
        return [ev["raw"], ""]
    ts = ev.get("ts", "?")
    wid = ev.get("worker", "?")
    event = ev.get("event", "?")
    detail = ev.get("detail", "")
    phase = ev.get("phase", "")
    tel = ev.get("telemetry_age_ms", "")
    err = ev.get("last_error", "")

    lines = []
    lines.append("{0}  worker={1}  EVENT={2}".format(ts, wid, event.upper()))
    if detail:
        lines.append("    raison : {0}".format(detail))
    meta = []
    if phase:
        meta.append("phase={0}".format(phase))
    if tel is not None and tel != "":
        meta.append("telemetry_age_ms={0}".format(tel))
    if err:
        meta.append("last_error={0}".format(err))
    if meta:
        lines.append("    " + "  ".join(meta))

    if event in _KILL_EVENTS and tail_n > 0:
        logs = _worker_logs(wid)
        for label, path in logs.items():
            if not path:
                continue
            tail = _tail(path, tail_n)
            if tail:
                lines.append("    --- derniers logs {0} : {1}".format(label, os.path.basename(path)))
                for ln in tail:
                    lines.append("        " + ln.rstrip("\n"))
                lines.append("")
    return lines


def _report(tail_n):
    events = _read_events()
    kills = [e for e in events if e.get("event") in _KILL_EVENTS]
    print("=== WORKER CRASH REPORT ===")
    print("journal : {0}".format(_EVENTS))
    print("événements : {0}   fermetures/kills : {1}".format(len(events), len(kills)))
    print()
    if not kills:
        print("Aucune fermeture de worker enregistrée pour l'instant.")
        print("(Lance les 6 workers ; dès qu'un worker se referme, son événement apparaît ici.)")
        return
    for ev in kills:
        for line in _render_event(ev, tail_n):
            print(line)
        print("-" * 72)


def _watch(tail_n):
    print("Suivi en direct de {0}".format(_EVENTS))
    print("(Ctrl-C pour arrêter) Lance les 6 workers et regarde ici.\n")
    seen = 0
    while True:
        events = _read_events()
        if len(events) > seen:
            for ev in events[seen:]:
                for line in _render_event(ev, tail_n):
                    print(line)
                print("-" * 72)
            seen = len(events)
        time.sleep(1.0)


def main():
    ap = argparse.ArgumentParser(description="Debug des fermetures de workers.")
    ap.add_argument("--tail", type=int, default=30, help="lignes de log à montrer par kill (0 = aucune)")
    ap.add_argument("--watch", action="store_true", help="suivre en direct au lieu d'un rapport unique")
    ap.add_argument("--clear", action="store_true", help="vider le journal avant de suivre")
    args = ap.parse_args()

    if args.clear and os.path.exists(_EVENTS):
        os.remove(_EVENTS)
        print("Journal vidé.")

    if args.watch:
        try:
            _watch(args.tail)
        except KeyboardInterrupt:
            print("\nArrêt.")
    else:
        _report(args.tail)


if __name__ == "__main__":
    main()
