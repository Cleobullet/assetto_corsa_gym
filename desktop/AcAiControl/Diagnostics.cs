using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace AcAiControl;

internal sealed class DiagWorker
{
    public int Id { get; init; }
    public string Phase { get; init; } = "absent";
    public bool CollectorAlive { get; init; }
    public int CollectorPid { get; init; }
    public double TelemetryAgeMs { get; init; } = double.PositiveInfinity;
    public long Transitions { get; init; }
    public long TelemetryFrameCount { get; init; }
    public double SpeedKmh { get; init; }
    public int EpisodeCount { get; init; }
    public double Reward { get; init; }
    public bool AcPidKnown { get; init; }
    public int NeuroCandidate { get; init; } = -1;
    public double NeuroScore { get; init; } = double.NegativeInfinity;
    public int NeuroLaps { get; init; }
    public double NeuroProgressM { get; init; }
}

internal sealed class NeuroSummary
{
    public int Generation { get; init; }
    public int Evaluated { get; init; }
    public int PopulationSize { get; init; }
    public int BestIndex { get; init; } = -1;
    public double BestScore { get; init; } = double.NegativeInfinity;
}

internal static class Diagnostics
{
    public const int MaxWorkers = 6;

    private static readonly object Lock = new();
    private static DateTime _lastSampleAt = DateTime.MinValue;
    private static readonly Dictionary<int, (DateTime At, TimeSpan Cpu)> _cpu = new();
    private static (double Total, Dictionary<int, double> PerWorker) _lastCpuResult = (0.0, new Dictionary<int, double>());
    private static Dictionary<int, int> _acPids = new();
    private static DateTime _acPidsAt = DateTime.MinValue;

    /// <summary>Maps workerId -> game process (acs.exe) PID, read from the C4D manifest (best effort). Cached ~2 s.</summary>
    public static Dictionary<int, int> ReadAcPids()
    {
        var now = DateTime.UtcNow;
        if (_acPidsAt != DateTime.MinValue && (now - _acPidsAt).TotalSeconds < 2.0)
            return _acPids;

        var map = new Dictionary<int, int>();
        try
        {
            if (File.Exists(RepoPaths.Manifest))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(RepoPaths.Manifest));
                if (doc.RootElement.TryGetProperty("workers", out var workers)
                    && workers.ValueKind == JsonValueKind.Array)
                {
                    foreach (var w in workers.EnumerateArray())
                    {
                        if (w.TryGetProperty("workerId", out var wid) && wid.ValueKind == JsonValueKind.Number
                            && w.TryGetProperty("acPid", out var pid) && pid.ValueKind == JsonValueKind.Number)
                            map[wid.GetInt32()] = pid.GetInt32();
                    }
                }
            }
        }
        catch
        {
            // manifest absent ou illisible : on s'en passe
        }

        // Le manifeste ne garde que le dernier worker lancé. On complète via le
        // titre des fenêtres du jeu (« Assetto Corsa Worker N »), plus fiable.
        try
        {
            foreach (var name in new[] { "acs", "AssettoCorsa" })
            {
                foreach (var p in Process.GetProcessesByName(name))
                {
                    try
                    {
                        var m = Regex.Match(p.MainWindowTitle ?? "", @"Worker\s+(\d+)");
                        if (m.Success && int.TryParse(m.Groups[1].Value, out var wid)
                            && wid >= 0 && wid < MaxWorkers && !map.ContainsKey(wid))
                            map[wid] = p.Id;
                    }
                    catch { /* fenêtre inaccessible */ }
                    finally { try { p.Dispose(); } catch { } }
                }
            }
        }
        catch { /* énumération échouée */ }

        _acPids = map;
        _acPidsAt = now;
        return _acPids;
    }

    public static List<DiagWorker> ReadWorkers()
    {
        _acPids = ReadAcPids();
        var workers = new List<DiagWorker>();
        for (var id = 0; id < MaxWorkers; id++)
        {
            var w = new DiagWorker { Id = id };
            var path = Path.Combine(RepoPaths.StatusDir, $"worker_{id}.json");
            if (!File.Exists(path))
            {
                workers.Add(w);
                continue;
            }
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(path));
                var r = doc.RootElement;
                var collectorPid = GetInt(r, "collector_pid");
                var speed = 0.0;
                if (r.TryGetProperty("telemetry_snapshot", out var ts) && ts.ValueKind == JsonValueKind.Object)
                    speed = GetDouble(ts, "speed", 0.0) * 3.6;
                w = new DiagWorker
                {
                    Id = id,
                    Phase = GetString(r, "phase", "absent"),
                    CollectorAlive = ProcessWatch.PidAlive(collectorPid),
                    CollectorPid = collectorPid,
                    TelemetryAgeMs = GetDouble(r, "telemetry_age_ms", double.PositiveInfinity),
                    Transitions = GetLong(r, "transition_count"),
                    TelemetryFrameCount = GetLong(r, "telemetry_frame_count"),
                    SpeedKmh = speed,
                    EpisodeCount = GetInt(r, "episode_count"),
                    Reward = GetDouble(r, "last_reward", 0.0),
                    AcPidKnown = _acPids.ContainsKey(id),
                    NeuroCandidate = GetInt(r, "neuroevolution_candidate", -1),
                    NeuroScore = GetDouble(r, "neuroevolution_score", double.NegativeInfinity),
                    NeuroLaps = GetInt(r, "neuroevolution_laps"),
                    NeuroProgressM = GetDouble(r, "neuroevolution_progress_m", 0.0),
                };
            }
            catch
            {
                // fichier en cours d'écriture : on garde les valeurs par défaut
            }
            workers.Add(w);
        }
        return workers;
    }

    /// <summary>Récompense moyenne globale (learner), lue depuis learner_status.json.</summary>
    public static double ReadMeanReward()
    {
        try
        {
            if (File.Exists(RepoPaths.LearnerStatus))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(RepoPaths.LearnerStatus));
                var r = doc.RootElement;
                if (r.TryGetProperty("mean_reward", out var v) && v.ValueKind == JsonValueKind.Number)
                    return v.GetDouble();
            }
        }
        catch { /* learner pas encore démarré */ }
        return 0.0;
    }

    /// <summary>Résumé de la neuroévolution (génération, candidats évalués, meilleur).</summary>
    public static NeuroSummary ReadNeuroEvolution()
    {
        try
        {
            var path = Path.Combine(RepoPaths.Root, "outputs", "neuroevolution", "summary.json");
            if (!File.Exists(path)) return new NeuroSummary();
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var r = doc.RootElement;
            return new NeuroSummary
            {
                Generation = GetInt(r, "generation"),
                Evaluated = GetInt(r, "evaluated"),
                PopulationSize = GetInt(r, "population_size"),
                BestIndex = GetInt(r, "best_index", -1),
                BestScore = GetDouble(r, "best_score", double.NegativeInfinity),
            };
        }
        catch { return new NeuroSummary(); }
    }

    /// <summary>Sample CPU over the elapsed interval. Returns (totalCpuPercent, perWorkerCpuPercent).</summary>
    public static (double Total, Dictionary<int, double> PerWorker) SampleCpu(IReadOnlyList<DiagWorker> workers)
    {
        var now = DateTime.UtcNow;
        var perWorker = new Dictionary<int, double>();
        lock (Lock)
        {
            // Énumérer tous les processus est la partie lente : on ne le fait qu'une
            // fois par seconde et on réutilise le dernier résultat entre-temps.
            if (_lastSampleAt != DateTime.MinValue && (now - _lastSampleAt).TotalSeconds < 1.0)
                return _lastCpuResult;

            // Instantané des temps CPU actuels de tous les processus.
            var current = new Dictionary<int, TimeSpan>();
            try
            {
                foreach (var p in Process.GetProcesses())
                {
                    try { current[p.Id] = p.TotalProcessorTime; } catch { }
                    try { p.Dispose(); } catch { }
                }
            }
            catch { /* énumération échouée */ }

            if (_lastSampleAt == DateTime.MinValue)
            {
                foreach (var (pid, cpu) in current)
                    _cpu[pid] = (now, cpu);
                _lastSampleAt = now;
                _lastCpuResult = (0.0, perWorker);
                return _lastCpuResult;
            }

            var elapsed = (now - _lastSampleAt).TotalSeconds;
            _lastSampleAt = now;
            if (elapsed <= 0.05)
                return _lastCpuResult;

            var cores = Math.Max(1, Environment.ProcessorCount);

            // Delta de CPU par PID (cœurs utilisés), calculé AVANT de mettre à jour
            // l'échantillon de référence (sinon le CPU par worker retombe à zéro).
            var delta = new Dictionary<int, double>();
            foreach (var (pid, cpu) in current)
            {
                if (_cpu.TryGetValue(pid, out var prev))
                    delta[pid] = (cpu - prev.Cpu).TotalSeconds / elapsed;
            }

            // CPU totale (tous les processus).
            var total = delta.Values.Sum() / cores * 100.0;

            // CPU par worker : collector + jeu acs.exe.
            foreach (var w in workers)
            {
                var coresUsed = 0.0;
                if (w.CollectorPid > 0 && delta.TryGetValue(w.CollectorPid, out var c1))
                    coresUsed += c1;
                if (_acPids.TryGetValue(w.Id, out var acPid) && acPid > 0 && delta.TryGetValue(acPid, out var c2))
                    coresUsed += c2;
                perWorker[w.Id] = coresUsed / cores * 100.0;
            }

            // Mise à jour de l'échantillon de référence.
            foreach (var (pid, cpu) in current)
                _cpu[pid] = (now, cpu);

            _lastCpuResult = (total, perWorker);
            return _lastCpuResult;
        }
    }

    public static string Recommendation(double totalCpu, IReadOnlyList<DiagWorker> workers)
    {
        var running = workers.Where(w => w.CollectorAlive).ToList();
        if (running.Count == 0)
            return "Aucune voiture lancée. Clique « Démarrer les workers » pour commencer.";

        var anyStale = running.Any(w => w.TelemetryAgeMs > 1500);
        var anyError = running.Any(w => w.Phase.Contains("error", StringComparison.OrdinalIgnoreCase)
                                        || w.Phase.Contains("stuck", StringComparison.OrdinalIgnoreCase));
        if (anyError)
            return "⚠️ Une voiture est en erreur (voir sa barre rouge). Règle ce problème avant d'en ajouter une.";
        if (anyStale)
            return "⚠️ Une voiture ne répond plus (télémétrie trop vieille). Vérifie-la avant d'en ajouter.";

        if (running.Count >= MaxWorkers)
            return "✅ Toutes les voitures possibles sont déjà lancées (6/6).";

        if (totalCpu >= 85)
            return "🔴 L'ordinateur est très chargé (" + totalCpu.ToString("0") + " %). N'ajoute PAS de voiture.";
        if (totalCpu < 60)
            return "🟢 Il reste de la marge (" + totalCpu.ToString("0") + " %). Tu peux ajouter une voiture.";
        return "🟡 Charge moyenne (" + totalCpu.ToString("0") + " %). Tu peux rester comme ça, ajouter une voiture est limite.";
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

    private static double GetDouble(JsonElement el, string name, double fallback)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return fallback;
        return v.ValueKind switch
        {
            JsonValueKind.Number => v.GetDouble(),
            JsonValueKind.String => double.TryParse(v.GetString(), System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out var d) ? d : fallback,
            _ => fallback,
        };
    }

    private static string GetString(JsonElement el, string name, string fallback)
    {
        if (el.ValueKind != JsonValueKind.Object || !el.TryGetProperty(name, out var v)) return fallback;
        return v.ValueKind == JsonValueKind.String ? (v.GetString() ?? fallback) : v.ToString();
    }
}
