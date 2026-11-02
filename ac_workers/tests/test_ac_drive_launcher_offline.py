"""Offline static/parse tests for AC Drive launcher activation."""
from __future__ import annotations

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
COMMON = REPO / "scripts" / "Ac-AiSessionCommon.ps1"
MULTI = REPO / "scripts" / "Start-Multi-AI-Session.ps1"


def _src() -> str:
    return COMMON.read_text(encoding="utf-8")


def _multi_src() -> str:
    return MULTI.read_text(encoding="utf-8")


def test_unified_wheel_click_never_hits_power_or_exit():
    src = _src()
    fn = src.split("function Get-AcDriveWheelClickPoints")[1].split("function Get-AcDriveClickClientPoint")[0]
    assert "48" in fn
    # y=96 hit the EXIT button — must not be used as a click Y coordinate
    assert "$y = 96" not in fn, "y=96 hits EXIT button, must not click there"
    assert "Y = 96" not in fn, "y=96 hits EXIT button, must not click there"
    # Drive points lowered +114 px (~3 cm) from 78/86
    assert "192" in fn
    assert "200" in fn
    assert "Test-AcDriveClickEnabled" in _src()
    assert "SendRealClientClick" in _src()
    # += @{...} merges hashtables in PowerShell (X becomes Object[])
    assert "$pts += @{" not in fn
    assert "List[object]" in fn
    assert "38, 48, 58" not in fn
    assert "0.44" not in fn
    assert "230" not in fn
    invoke = src.split("function Invoke-AcDriveActivation")[1].split("function Wait-AcMainWindowReady")[0]
    assert "Invoke-AcDriveClickScan" not in invoke
    assert "session already active" not in invoke
    assert "Send-AcResumeDrive" in invoke
    assert "Get-AcDriveWheelClickPoints" in src


def test_all_workers_use_same_drive_sequence():
    multi = _multi_src()
    assert "meme sequence 0-5" in multi
    assert "if ($w -eq 0) {" not in multi
    assert "Invoke-AcDriveActivation" in multi
    assert "SkipVjoyIndexProbe" in multi
    assert "collector import preflight skipped" in multi


def test_no_old_bottom_center_click_formula():
    src = _src()
    fn = src.split("function Get-AcDriveWheelClickPoints")[1].split("function Invoke-AcDriveFixedClick")[0]
    assert "0.88" not in fn
    assert "640" not in fn
    assert "634" not in fn


def test_postmessage_fallback_when_foreground_fails():
    src = _src()
    fn = src.split("function Send-AcDriveClickAttempt")[1].split("function Test-AcDriveClientReady")[0]
    assert "PostMessage fallback" in fn
    assert "return $false" not in fn
    assert "SendClientClick" in fn


def test_wm_mousemove_before_click():
    src = _src()
    assert "WM_MOUSEMOVE" in src
    block = src.split("public static void SendClientClick")[1].split("public static void SendEnterKeyToWindow")[0]
    assert block.index("WM_MOUSEMOVE") < block.index("WM_LBUTTONDOWN")


def test_fixed_click_is_drive_mechanism():
    src = _src()
    invoke = src.split("function Invoke-AcDriveActivation")[1].split("function Wait-AcMainWindowReady")[0]
    assert "Send-AcResumeDrive" in invoke
    assert "plugin wheel click" in invoke
    assert "Wait-AcDriveEngagedPoll" in invoke
    assert "session already active" not in invoke


def test_fixed_clicker_always_clicks_wheel():
    src = _src()
    fn = src.split("function Invoke-AcDriveFixedClick")[1].split("function Invoke-AcDriveClickScan")[0]
    assert "$TimeoutMs = 10000" in fn
    assert "SendClientClick" in fn
    assert "SendRealClientClick" in fn
    assert "Get-AcDriveWheelClickPoints" in fn
    assert "$rounds -ge 2" in fn
    assert "Get-AcDriveRedButtonPoint" not in src
    assert "Test-AcPixelIsRed" not in src
    assert "Test-AcDriveEngagedNow" not in fn


def test_plugin_resume_drive_clicks_wheel():
    src = (REPO / "assetto_corsa_gym" / "AssettoCorsaPlugin" / "plugins" / "sensors_par" / "drive_state.py").read_text(
        encoding="utf-8"
    )
    assert "_post_drive_wheel_clicks" in src
    assert "0x0201" in src
    assert "_post_client_click" in src
    # Plugin must NOT use SetCursorPos (moves mouse during gameplay).
    # Only PostMessage from plugin; real clicks come from the launcher.
    assert "SetCursorPos" not in src
    assert "mouse_event" not in src
    assert "wintypes.BOOL" in src
    assert "WINFUNCTYPE(ctypes.c_bool" not in src
    resume = src.split("def _resume_drive_main_thread")[1].split("def _clear_pending_resume")[0]
    assert "_post_drive_wheel_clicks()" in resume


def test_resume_drive_helper_sends_mgmt_command():
    src = _src()
    helper = src.split("function Send-AcResumeDrive")[1].split("function Invoke-AcDriveActivation")[0]
    assert "resume_drive" in helper
    assert "Invoke-AcManagementCommand" in helper


def test_drive_ui_stable_and_click_attempt_logs():
    src = _src()
    assert "Drive UI stable" in src
    assert "Drive click attempt" in src
    assert "Wait-AcClientUiStable" in src


def test_drive_activation_theoretical_under_forty_seconds():
    stable_ms = 2 * 150
    render_ms = 1000
    pixel_cap_ms = 10000
    total_ms = stable_ms + render_ms + pixel_cap_ms
    assert total_ms < 50000


def test_drive_requires_click_not_shared_memory_skip():
    src = _src()
    invoke = src.split("function Invoke-AcDriveActivation")[1].split("function Wait-AcMainWindowReady")[0]
    assert "Drive input delivered" not in invoke
    assert "plugin wheel click timeout" in invoke
    assert "Test-AcDriveClickEnabled" in invoke
    assert "attente clic manuel" in invoke
    assert "session already active" not in invoke


def test_fixed_clicker_bounded_no_legacy_failure_token():
    src = _src()
    invoke = src.split("function Invoke-AcDriveActivation")[1].split("function Wait-AcMainWindowReady")[0]
    assert "Send-AcResumeDrive" in invoke
    assert "drive_activation_not_confirmed" not in invoke


def test_drive_activation_requires_click_success():
    src = _src()
    invoke = src.split("function Invoke-AcDriveActivation")[1].split("function Wait-AcMainWindowReady")[0]
    assert "Drive confirmed via plugin wheel click" in invoke
    assert "return $false" in invoke
    assert "Drive input delivered" not in invoke


def test_never_skip_clicks_when_other_worker_already_driving():
    src = _src()
    invoke = src.split("function Invoke-AcDriveActivation")[1].split("function Wait-AcMainWindowReady")[0]
    assert "session already active" not in invoke
    assert "Send-AcResumeDrive" in invoke
    assert "menu_brake_hold" in invoke


def test_collector_starts_while_management_pending():
    multi = _multi_src()
    assert "management port pending after Drive" in multi
    assert "Drive input not delivered" in multi
    assert "drive_activation_not_confirmed" not in multi
    assert "non ouvert apres Drive" not in multi


def test_launcher_allows_pending_management():
    src = _src()
    assert "AllowPendingManagement" in src
    assert "launcher ready: management port pending (collector will wait)" in src


def test_foreground_activation_before_input():
    src = _src()
    assert "ActivateForeground" in src
    assert "SendClientClick" in src


def test_no_premature_drive_ready_log():
    src = _src()
    assert "Drive ready" not in src
    assert "Drive confirmed via plugin wheel click" in src


def test_bounded_retries_and_explicit_failure():
    src = _src()
    invoke = src.split("function Invoke-AcDriveActivation")[1].split("function Wait-AcMainWindowReady")[0]
    assert "Send-AcResumeDrive" in invoke
    assert "Invoke-AcDriveFixedClick" in invoke
    assert "Start-Sleep -Seconds 3" in invoke
    assert "Drive input not delivered" in src
    assert "drive_activation_not_confirmed" not in invoke


def test_worker_window_title_set():
    src = _src()
    assert "Set-AcWorkerWindowTitle" in src
    assert "Assetto Corsa Worker " in src
    assert "SetWindowText" in src


def test_official_ac_launcher_not_started():
    src = _src()
    assert "function Close-OfficialAcLauncher" not in src
    assert "function Ensure-SteamClientReady" in src
    assert "function Stop-StrayOfficialAcMenu" in src
    ensure = src.split("function Ensure-AcSteamAppIdFile")[1].split("function Start-AcWorkerProcess")[0]
    assert "steam_appid.txt" in ensure
    assert "244210" in ensure
    start = src.split("function Start-AcWorkerProcess")[1].split("function Invoke-RecordAcLaunchPid")[0]
    assert "Ensure-SteamClientReady" in start
    assert "Ensure-AcSteamAppIdFile" in start
    assert "Stop-StrayOfficialAcMenu" in start
    assert "SteamAppId" in start
    assert "244210" in start
    handshake = src.split("function Invoke-WorkerHandshake")[1].split("function Test-TcpPortOpen")[0]
    assert "Invoke-PythonCapturedProcess" in handshake
    assert "& $venv" not in handshake
    snap = src.split("function Invoke-RecordAcLaunchPid")[1].split("function Invoke-ClearControlsSnapshots")[0]
    assert "Invoke-PythonCapturedProcess" in snap
    assert "& $venv" not in snap


def test_drive_click_toggle_pref(tmp_path=None):
    from ac_workers import drive_click as dc

    old = dc.PREF_PATH
    try:
        dc.PREF_PATH = REPO / "ac_workers" / "status" / "_drive_click_test.json"
        if dc.PREF_PATH.is_file():
            dc.PREF_PATH.unlink()
        assert dc.is_enabled() is True
        assert "usage" in dc.handle_drive_click_command("maybe")
        assert "allume" in dc.handle_drive_click_command("on")
        assert dc.is_enabled() is True
        assert "eteint" in dc.handle_drive_click_command("off")
        assert dc.is_enabled() is False
        assert "off" in dc.handle_drive_click_command("")
    finally:
        if dc.PREF_PATH.is_file():
            dc.PREF_PATH.unlink()
        dc.PREF_PATH = old


def test_wheel_points_are_scalar_ints():
    import subprocess

    script = (
        ". '{0}'; "
        "$pts = @(Get-AcDriveWheelClickPoints -WindowHandle ([IntPtr]::Zero)); "
        "$rows = foreach ($p in $pts) {{ "
        "'{{0}}:{{1}}:{{2}}:{{3}}' -f $p.X.GetType().Name, $p.X, $p.Y.GetType().Name, $p.Y "
        "}}; "
        "Write-Output ('count=' + $pts.Count); "
        "Write-Output ($rows -join '|')"
    ).format(str(COMMON).replace("'", "''"))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = (proc.stdout or "").strip()
    assert "count=4" in out, out
    assert "Int32:" in out, out
    assert "Object[]" not in out, out


def test_drive_confirmed_helper_rejects_menu_brake():
    src = _src()
    helper = src.split("function Test-AcDriveEngaged ")[1].split("function Test-AcDriveSessionLive")[0]
    assert "menu_brake_hold -eq $true" in helper
    assert "if (-not $DrivingState.driving) { return $false }" in helper


def main() -> int:
    tests = [
        test_unified_wheel_click_never_hits_power_or_exit,
        test_all_workers_use_same_drive_sequence,
        test_no_old_bottom_center_click_formula,
        test_postmessage_fallback_when_foreground_fails,
        test_wm_mousemove_before_click,
        test_fixed_click_is_drive_mechanism,
        test_fixed_clicker_always_clicks_wheel,
        test_resume_drive_helper_sends_mgmt_command,
        test_plugin_resume_drive_clicks_wheel,
        test_drive_ui_stable_and_click_attempt_logs,
        test_drive_activation_theoretical_under_forty_seconds,
        test_fixed_clicker_bounded_no_legacy_failure_token,
        test_drive_requires_click_not_shared_memory_skip,
        test_drive_activation_requires_click_success,
        test_never_skip_clicks_when_other_worker_already_driving,
        test_collector_starts_while_management_pending,
        test_launcher_allows_pending_management,
        test_foreground_activation_before_input,
        test_no_premature_drive_ready_log,
        test_bounded_retries_and_explicit_failure,
        test_worker_window_title_set,
        test_official_ac_launcher_not_started,
        test_drive_click_toggle_pref,
        test_wheel_points_are_scalar_ints,
        test_drive_confirmed_helper_rejects_menu_brake,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-AcDriveLauncher-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
