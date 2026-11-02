#Requires -Version 5.1
<#
.SYNOPSIS
    Test hors ligne transition server (2 faux collectors + learner + atomic publish).
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$env:PYTHONPATH = $RepoRoot

$py = @'
import threading
import time
from common.transition_server import TransitionServer, TransitionClient, atomic_publish_checkpoint
from pathlib import Path

s = TransitionServer(port=24051)
s.start()
time.sleep(0.2)

def collector(wid):
    c = TransitionClient(port=24051)
    for i in range(3):
        c.push_transition({"worker_id": wid, "episode_id": 0, "step_id": i, "reward": 0.0})
        c.heartbeat(wid)

threads = [threading.Thread(target=collector, args=(0,)), threading.Thread(target=collector, args=(1,))]
for t in threads:
    t.start()
for t in threads:
    t.join()

c = TransitionClient(port=24051)
c.learner_ping()
batch = c.pull_batch(10)
assert len(batch) == 6, batch
stats = c.stats()
assert stats["workers"]["0"]["transitions_received"] == 3
assert stats["workers"]["1"]["transitions_received"] == 3

root = Path(r"__REPO_ROOT__")
src = root / "ac_workers" / "_test_weights.bin"
dst = root / "ac_workers" / "_test_weights_latest.bin"
src.parent.mkdir(parents=True, exist_ok=True)
src.write_bytes(b"ok")
atomic_publish_checkpoint(src, dst)
assert dst.read_bytes() == b"ok"
print("Test-TransitionServer: OK")
time.sleep(0.3)
s.stop()
'@ -replace '__REPO_ROOT__', ($RepoRoot -replace '\\', '\\')

$tmp = Join-Path $env:TEMP ('test_transition_' + [guid]::NewGuid().ToString('N') + '.py')
Set-Content -LiteralPath $tmp -Value $py -Encoding UTF8
try {
    & $VenvPython $tmp
    exit $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}
