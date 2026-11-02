using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;

namespace AcAiControl;

internal sealed class WorkerSnapshot
{
    public int Id { get; init; }
    public int DisplayId => Id + 1;
    public string Phase { get; init; } = "absent";
    public double? TelemetryAgeMs { get; init; }
    public string Speed { get; init; } = "—";
    public string Gas { get; init; } = "—";
    public string Brake { get; init; } = "—";
    public string Steer { get; init; } = "—";
    public long Transitions { get; init; }
    public string SelectedModel { get; init; } = "—";
    public string Error { get; init; } = "";
    public int CollectorPid { get; init; }
    public bool CollectorAlive { get; init; }
}

internal sealed class LearnerSnapshot
{
    public int Pid { get; init; }
    public bool Alive { get; init; }
    public long GlobalSteps { get; init; }
    public long Updates { get; init; }
    public double AgeSec { get; init; }
}

internal static class ProcessWatch
{
    public static bool PidAlive(int pid)
    {
        if (pid <= 0) return false;
        try
        {
            using var p = Process.GetProcessById(pid);
            return !p.HasExited;
        }
        catch
        {
            return false;
        }
    }

    public static LearnerSnapshot ReadLearner()
    {
        var data = ReadJson(RepoPaths.LearnerStatus);
        var pid = GetInt(data, "learner_pid");
        var ts = GetDouble(data, "ts");
        var age = ts > 0 ? Math.Max(0, DateTimeOffset.UtcNow.ToUnixTimeSeconds() - ts) : 0;
        return new LearnerSnapshot
        {
            Pid = pid,
            Alive = PidAlive(pid),
            GlobalSteps = GetLong(data, "global_steps"),
            Updates = GetLong(data, "updates"),
            AgeSec = age,
        };
    }

    public static WorkerSnapshot ReadWorker(int id)
    {
        var path = Path.Combine(RepoPaths.StatusDir, $"worker_{id}.json");
        var data = ReadJson(path);
        var pid = GetInt(data, "collector_pid");
        if (!PidAlive(pid))
            return new WorkerSnapshot { Id = id, Phase = pid > 0 ? "arrêté" : "absent", CollectorPid = pid, CollectorAlive = false, TelemetryAgeMs = null, SelectedModel = "—" };
        string speed = "—", gas = "—", brake = "—", steer = "—";
        if (data.ValueKind == JsonValueKind.Object &&
            data.TryGetProperty("telemetry_snapshot", out var tel) &&
            tel.ValueKind == JsonValueKind.Object)
        {
            speed = FormatVal(tel, "speed");
            gas = FormatVal(tel, "gas");
            brake = FormatVal(tel, "brake");
            steer = FormatSteer(tel);
        }

        var modelError = GetString(data, "policy_load_error", "");
        var ready = data.ValueKind == JsonValueKind.Object && data.TryGetProperty("policy_ready", out var pr) && pr.ValueKind == JsonValueKind.True;
        var step = GetLong(data, "policy_training_step");
        var nevoCand = GetInt(data, "neuroevolution_candidate", -1);
        var sessionName = ReadSessionName();
        string model;
        if (sessionName.Length > 0)
            model = sessionName;
        else if (nevoCand >= 0)
            model = $"Candidat #{nevoCand}";
        else
            model = ready ? (step == 0 ? "Chargé • 0 étape" : $"Chargé • {step} étapes") : (modelError.Length > 0 ? "Refusé • incompatible" : "Aucun modèle chargé");
        return new WorkerSnapshot
        {
            Id = id,
            Phase = GetString(data, "phase", "absent"),
            TelemetryAgeMs = GetDouble(data, "telemetry_age_ms"),
            Speed = speed,
            Gas = gas,
            Brake = brake,
            Steer = steer,
            Transitions = GetLong(data, "transition_count"),
            SelectedModel = pid > 0 ? model : "—",
            Error = !ready && modelError.Length > 0 ? modelError : GetString(data, "last_error", ""),
            CollectorPid = pid,
            CollectorAlive = PidAlive(pid),
        };
    }

    public static string ReadSessionName()
    {
        try
        {
            var path = Path.Combine(RepoPaths.Root, "outputs", "training_sessions", "current.json");
            if (!File.Exists(path)) return "";
            var data = ReadJson(path);
            return GetString(data, "name", "");
        }
        catch
        {
            return "";
        }
    }

    public static int CountAliveWorkers(int max = 6)
    {
        var n = 0;
        for (var i = 0; i < max; i++)
            if (ReadWorker(i).CollectorAlive) n++;
        return n;
    }

    /// <summary>Reads multi_session logs written after <paramref name="sinceUtc"/> for FATAL.</summary>
    public static string? LatestSessionFatal(DateTime? sinceUtc = null)
    {
        try
        {
            var logs = Path.Combine(RepoPaths.Root, "ac_workers", "logs");
            if (!Directory.Exists(logs)) return null;
            var cutoff = sinceUtc ?? DateTime.UtcNow.AddMinutes(-2);
            var newest = new DirectoryInfo(logs)
                .GetFiles("multi_session_*.log")
                .Where(f => f.LastWriteTimeUtc >= cutoff)
                .OrderByDescending(f => f.LastWriteTimeUtc)
                .FirstOrDefault();
            if (newest is null) return null;
            foreach (var line in File.ReadLines(newest.FullName))
            {
                if (line.Contains("FATAL", StringComparison.OrdinalIgnoreCase))
                    return line.Trim();
            }
        }
        catch
        {
            // ignore
        }
        return null;
    }

    public static async Task WaitUntilAsync(
        Func<bool> predicate,
        TimeSpan timeout,
        Action<string>? progress = null,
        CancellationToken ct = default,
        Func<string?>? failure = null)
    {
        var start = DateTime.UtcNow;
        while (!predicate())
        {
            ct.ThrowIfCancellationRequested();
            var fail = failure?.Invoke();
            if (!string.IsNullOrWhiteSpace(fail))
                throw new InvalidOperationException(fail);
            var elapsed = DateTime.UtcNow - start;
            if (elapsed > timeout)
                throw new TimeoutException($"Délai dépassé après {elapsed.TotalSeconds:0}s.");
            progress?.Invoke($"En cours… {elapsed.TotalSeconds:0}s");
            await Task.Delay(500, ct).ConfigureAwait(false);
        }
    }

    public static void RunEmergencyStop(Action<string>? log = null)
    {
        var script = RepoPaths.EmergencyStopScript;
        if (!File.Exists(script))
            throw new FileNotFoundException("Emergency-Stop-AI.ps1 introuvable.", script);

        var psi = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            // -CloseGame: ferme aussi chaque fenêtre acs.exe (workers).
            Arguments = $"-NoProfile -ExecutionPolicy Bypass -File \"{script}\" -CloseGame",
            WorkingDirectory = RepoPaths.Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        using var proc = Process.Start(psi) ?? throw new InvalidOperationException("Impossible de lancer l'arrêt d'urgence.");
        var stdoutTask = proc.StandardOutput.ReadToEndAsync();
        var stderrTask = proc.StandardError.ReadToEndAsync();
        if (!proc.WaitForExit(120_000))
            throw new TimeoutException("Arrêt d’urgence non confirmé après 120 secondes. Vérifie les processus avant de redémarrer.");
        var stdout = stdoutTask.GetAwaiter().GetResult();
        var stderr = stderrTask.GetAwaiter().GetResult();
        var output = stdout + "\n" + stderr;
        log?.Invoke(output.Trim());
        if (proc.ExitCode != 0 || output.Contains("INCOMPLETE", StringComparison.OrdinalIgnoreCase) || output.Contains("System health: DIRTY", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Arrêt d’urgence incomplet : des processus ou ports restent actifs. Consulte le journal.");

    }

    private static JsonElement ReadJson(string path)
    {
        try
        {
            if (!File.Exists(path))
                return default;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            return doc.RootElement.Clone();
        }
        catch
        {
            return default;
        }
    }

    private static int GetInt(JsonElement el, string name, int fallback = 0)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return fallback;
        return v.ValueKind switch
        {
            JsonValueKind.Number => v.TryGetInt32(out var i) ? i : fallback,
            JsonValueKind.String => int.TryParse(v.GetString(), out var i) ? i : fallback,
            _ => fallback,
        };
    }

    private static long GetLong(JsonElement el, string name)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return 0;
        return v.ValueKind switch
        {
            JsonValueKind.Number => v.TryGetInt64(out var i) ? i : 0,
            JsonValueKind.String => long.TryParse(v.GetString(), out var i) ? i : 0,
            _ => 0,
        };
    }

    private static double GetDouble(JsonElement el, string name)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return 0;
        return v.ValueKind switch
        {
            JsonValueKind.Number => v.GetDouble(),
            JsonValueKind.String => double.TryParse(v.GetString(), System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out var d) ? d : 0,
            _ => 0,
        };
    }

    private static string GetString(JsonElement el, string name, string fallback)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return fallback;
        return v.ValueKind == JsonValueKind.String ? (v.GetString() ?? fallback) : v.ToString();
    }

    private static string FormatVal(JsonElement el, string name)
    {
        if (!el.TryGetProperty(name, out var v) || v.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
            return "—";
        return v.ToString();
    }

    private static string FormatSteer(JsonElement tel)
    {
        if (!tel.TryGetProperty("steer", out var v) || v.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
            return "—";
        double deg;
        if (v.ValueKind == JsonValueKind.Number) deg = v.GetDouble();
        else if (!double.TryParse(v.GetString(), System.Globalization.NumberStyles.Float,
                     System.Globalization.CultureInfo.InvariantCulture, out deg)) return "—";
        if (Math.Abs(deg) < 2.0) return "—"; // volant au neutre
        return deg > 0 ? $"➡ {deg:0}°" : $"⬅ {Math.Abs(deg):0}°";
    }
}
