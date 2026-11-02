using System.Diagnostics;
using System.IO;
using System.Globalization;
using System.Text;
using System.Text.Json;

namespace AcAiControl;

internal sealed class RewardSearchRow
{
    public int Rank { get; init; }
    public string ScoreText { get; init; } = "";
    public string Mark { get; init; } = "";
    public string Progress { get; init; } = "";
    public string Offtrack { get; init; } = "";
    public string Terminal { get; init; } = "";
    public string Idle { get; init; } = "";
    public string LapBonus { get; init; } = "";
    public string Detail { get; init; } = "";
    public List<RewardField> Fields { get; init; } = new();
}

internal sealed class RewardSearchReport
{
    public string Summary { get; init; } = "";
    public List<RewardSearchRow> Rows { get; init; } = new();
}

internal static class RewardSearch
{
    public static Task<RewardSearchReport> RunAsync(CancellationToken ct, Action<string>? progress = null)
        => RunScriptAsync("reward_param_search.py", ct, progress);

    public static Task<RewardSearchReport> RunLiveAsync(CancellationToken ct, Action<string>? progress = null)
        => RunScriptAsync("reward_live_search.py", ct, progress);

    private static async Task<RewardSearchReport> RunScriptAsync(string scriptName, CancellationToken ct, Action<string>? progress)
    {
        var python = File.Exists(RepoPaths.Python) ? RepoPaths.Python : "python";
        var script = Path.Combine(RepoPaths.Root, "ac_workers", scriptName);
        if (!File.Exists(script))
            throw new FileNotFoundException("Recherche introuvable.", script);

        var start = new ProcessStartInfo(python)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = RepoPaths.Root,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        start.ArgumentList.Add(script);
        start.ArgumentList.Add("--config");
        start.ArgumentList.Add(RepoPaths.RewardConfig);
        start.Environment["PYTHONIOENCODING"] = "utf-8";

        using var process = Process.Start(start) ?? throw new InvalidOperationException("Python introuvable.");
        using var _ = ct.Register(() =>
        {
            try { process.Kill(entireProcessTree: true); } catch { /* already gone */ }
        });
        string? lastError = null;
        process.ErrorDataReceived += (_, e) =>
        {
            if (string.IsNullOrWhiteSpace(e.Data))
                return;
            lastError = e.Data;
            progress?.Invoke(e.Data);
        };
        process.BeginErrorReadLine();
        var stdoutTask = process.StandardOutput.ReadToEndAsync(ct);
        await process.WaitForExitAsync(ct);
        var stdout = await stdoutTask;
        if (process.ExitCode != 0)
        {
            var detail = lastError ?? "";
            if (detail.Length == 0 || detail.Contains("BESOIN_APPRENTISSAGE") || detail.StartsWith("Essai "))
                detail = "La recherche a échoué. Regarde le journal.";
            throw new InvalidOperationException(detail);
        }

        var line = stdout.Split('\n').Select(part => part.Trim()).LastOrDefault(part => part.StartsWith('{'));
        if (string.IsNullOrEmpty(line))
            throw new InvalidOperationException("La recherche n’a rien renvoyé.");

        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;
        var rows = new List<RewardSearchRow>();
        foreach (var row in root.GetProperty("rows").EnumerateArray())
        {
            var parms = row.GetProperty("params");
            var fields = new List<RewardField>();
            foreach (var (key, label, isBool) in RewardConfig.Fields)
            {
                if (!parms.TryGetProperty(key, out var value))
                    throw new InvalidOperationException("Paramètre manquant : " + key);
                fields.Add(new RewardField
                {
                    Key = key,
                    Label = label,
                    IsBool = isBool,
                    Value = Format(value),
                });
            }

            string Text(string key) => parms.TryGetProperty(key, out var value) ? Format(value) : "";
            rows.Add(new RewardSearchRow
            {
                Rank = row.GetProperty("rank").GetInt32(),
                ScoreText = row.GetProperty("score").GetDouble().ToString("0.00", CultureInfo.InvariantCulture),
                Mark = row.GetProperty("is_current").GetBoolean() ? "actuel" : "",
                Progress = Text("reward_progress_per_m"),
                Offtrack = Text("reward_offtrack_per_step"),
                Terminal = Text("reward_oot_terminal_penalty"),
                Idle = Text("reward_idle_penalty"),
                LapBonus = Text("reward_clean_lap_bonus"),
                Detail = row.GetProperty("detail").GetString() ?? "",
                Fields = fields,
            });
        }

        return new RewardSearchReport
        {
            Summary = root.GetProperty("summary").GetString() ?? "",
            Rows = rows,
        };
    }

    private static string Format(JsonElement value)
    {
        var number = value.GetDouble();
        if (Math.Abs(number - Math.Round(number)) < 1e-9 && Math.Abs(number) < 1e12)
            return ((long)Math.Round(number)).ToString(CultureInfo.InvariantCulture);
        return number.ToString("0.####", CultureInfo.InvariantCulture);
    }
}
