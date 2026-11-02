using System.Diagnostics;
using System.IO;
using System.Text.Json;

namespace AcAiControl;

internal static class ServerBootstrap
{
    public const string Host = "127.0.0.1";
    public const int Port = 24700;

    public static async Task<(C4dClient Client, string Token)> EnsureConnectedAsync(
        Action<string>? log = null,
        CancellationToken ct = default)
    {
        Directory.CreateDirectory(Path.Combine(RepoPaths.Root, "ac_workers", "logs"));
        Directory.CreateDirectory(Path.Combine(RepoPaths.Root, "ac_workers", ".c4d_server"));
        Directory.CreateDirectory(RepoPaths.StatusDir);

        var token = TryReadSessionToken();
        var client = new C4dClient();

        if (!string.IsNullOrEmpty(token))
        {
            try
            {
                await client.ConnectAsync(Host, Port, token, RepoPaths.Root, ct).ConfigureAwait(false);
                log?.Invoke("Connecté au centre de contrôle.");
                return (client, token);
            }
            catch (Exception ex)
            {
                log?.Invoke("Reconnexion… (" + ex.Message + ")");
                client.Dispose();
                client = new C4dClient();
            }
        }

        token = Guid.NewGuid().ToString("N");
        StartServer(token, log);

        Exception? last = null;
        for (var i = 0; i < 40; i++)
        {
            ct.ThrowIfCancellationRequested();
            await Task.Delay(500, ct).ConfigureAwait(false);
            var liveToken = TryReadSessionToken() ?? token;
            try
            {
                await client.ConnectAsync(Host, Port, liveToken, RepoPaths.Root, ct).ConfigureAwait(false);
                log?.Invoke("Connecté au centre de contrôle.");
                return (client, liveToken);
            }
            catch (Exception ex)
            {
                last = ex;
            }
        }

        throw new InvalidOperationException(
            "Le service ne démarre pas. Voir ac_workers/logs/windows_app_server.log. " +
            (last?.Message ?? ""));
    }

    private static void StartServer(string token, Action<string>? log)
    {
        var python = File.Exists(RepoPaths.Python) ? RepoPaths.Python : RepoPaths.Pythonw;
        if (!File.Exists(python))
            throw new FileNotFoundException("Python introuvable (.venv-acgym).", python);

        log?.Invoke("Démarrage du serveur C4D…");
        var psi = new ProcessStartInfo
        {
            FileName = python,
            // -E : ignore le PYTHONPATH hérité (Hermes injecte un numpy 2.x
            // incompatible avec Python 3.9) — le venv fournit ses propres
            // paquets via pyvenv.cfg. -B : pas de bytecode.
            Arguments = $"-E -B -m ac_workers.control_center server --repo-root \"{RepoPaths.Root}\" --session-token {token}",
            WorkingDirectory = RepoPaths.Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.Environment["PYTHONPATH"] = "";

        var proc = new Process { StartInfo = psi };
        proc.Start();

        _ = Task.Run(async () =>
        {
            try
            {
                await using var logFile = new StreamWriter(RepoPaths.ServerLog, append: true);
                var stdout = proc.StandardOutput.ReadToEndAsync();
                var stderr = proc.StandardError.ReadToEndAsync();
                await Task.WhenAll(stdout, stderr).ConfigureAwait(false);
                await logFile.WriteLineAsync("--- server " + DateTime.Now.ToString("s") + " ---").ConfigureAwait(false);
                await logFile.WriteAsync(await stdout.ConfigureAwait(false)).ConfigureAwait(false);
                await logFile.WriteAsync(await stderr.ConfigureAwait(false)).ConfigureAwait(false);
            }
            catch
            {
                // logging best-effort
            }
        });
    }

    private static string? TryReadSessionToken()
    {
        try
        {
            if (!File.Exists(RepoPaths.SessionFile))
                return null;
            using var doc = JsonDocument.Parse(File.ReadAllText(RepoPaths.SessionFile));
            if (doc.RootElement.TryGetProperty("token", out var t))
                return t.GetString();
        }
        catch
        {
            // ignore
        }
        return null;
    }
}
