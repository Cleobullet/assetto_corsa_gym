using System.IO;

namespace AcAiControl;

internal static class RepoPaths
{
    // Version affichée à côté du titre. À incrémenter manuellement (V1 -> V2 -> V3 …)
    // à CHAQUE modification du code : l'utilisateur lit ce numéro pour vérifier
    // que son raccourci lance bien la dernière build.
    public const string Version = "V23";

    public static string Root { get; } = FindRoot();

    public static string Pythonw => Path.Combine(Root, ".venv-acgym", "Scripts", "pythonw.exe");
    public static string Python => Path.Combine(Root, ".venv-acgym", "Scripts", "python.exe");
    public static string SessionFile => Path.Combine(Root, "ac_workers", ".c4d_server", "session.json");
    public static string StatusDir => Path.Combine(Root, "ac_workers", "status");
    public static string Manifest => Path.Combine(Root, "ac_workers", "manifest.json");
    public static string LearnerStatus => Path.Combine(Root, "outputs", "multi_sac_cpu_v3", "learner_status.json");
    public static string RewardConfig => Path.Combine(Root, "config_first_sac_cpu_v3.yml");
    public static string UiSettings => Path.Combine(Root, "config", "ui_settings.json");
    public static string EmergencyStopScript => Path.Combine(Root, "scripts", "Emergency-Stop-AI.ps1");
    public static string ServerLog => Path.Combine(Root, "ac_workers", "logs", "windows_app_server.log");

    private static string FindRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir != null)
        {
            var marker = Path.Combine(dir.FullName, "ac_workers", "control_center.py");
            var scripts = Path.Combine(dir.FullName, "scripts", "Start-Multi-AI-Session.ps1");
            if (File.Exists(marker) && File.Exists(scripts))
                return dir.FullName;
            dir = dir.Parent;
        }

        // Fallback: desktop/AcAiControl/bin/... -> repo root is 4 levels up from bin/Release/net8.0-windows
        var fallback = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
        if (File.Exists(Path.Combine(fallback, "ac_workers", "control_center.py")))
            return fallback;

        throw new InvalidOperationException(
            "Impossible de trouver la racine du projet (ac_workers/control_center.py manquant).");
    }
}
