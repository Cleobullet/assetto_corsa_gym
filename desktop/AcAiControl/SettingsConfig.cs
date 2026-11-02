using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using YamlDotNet.RepresentationModel;

namespace AcAiControl;

internal sealed class SettingField
{
    public string Key { get; init; } = "";
    public string Section { get; init; } = "AssettoCorsa";
    public string Label { get; init; } = "";
    public string Hint { get; init; } = "";
    public string Value { get; set; } = "";
    public bool IsBool { get; init; }
    public bool IsChoice { get; init; }
    public string[] Choices { get; init; } = Array.Empty<string>();

    public bool BoolValue
    {
        get
        {
            var t = Value.Trim();
            return t.Equals("true", StringComparison.OrdinalIgnoreCase)
                   || t == "1"
                   || t.Equals("yes", StringComparison.OrdinalIgnoreCase);
        }
        set => Value = value ? "True" : "False";
    }
}

/// <summary>
/// Paramètres d’entraînement (YAML) + préférences workers (JSON + race.ini / video.ini / audio.ini).
/// </summary>
internal static class SettingsConfig
{
    private static readonly string TracksConfigPath = Path.Combine(
        RepoPaths.Root, "assetto_corsa_gym", "AssettoCorsaConfigs", "tracks", "config.yaml");

    private static readonly string CarsDir = Path.Combine(
        RepoPaths.Root, "assetto_corsa_gym", "AssettoCorsaConfigs", "cars");

    public static readonly (string Key, string Section, string Label, string Hint, bool IsBool, Func<string[]>? Choices)[] YamlFields =
    {
        ("track", "AssettoCorsa", "Circuit (map)", "Circuits installés pour l’IA et disposant de données de piste et de trajectoire.", false, DiscoverTracks),
        ("car", "AssettoCorsa", "Voiture", "Voitures installées pour l’IA et disposant d’une calibration de direction.", false, DiscoverCars),
        ("device", "", "Ressources (device)", "CPU ou CUDA si disponible dans PyTorch. Pris en compte au prochain démarrage.", false, DiscoverDevices),
        ("action_exploration_noise_std", "Agent", "Bruit supplémentaire", "0 à 1. À zéro, SAC conserve son exploration propre ; seul le bruit ajouté est désactivé.", false, null),
        ("action_exploration_throttle_bias", "Agent", "Biais gaz (exploration)", "Décale le gaz pendant l’exploration (0 = neutre).", false, null),
        ("action_exploration_brake_bias", "Agent", "Biais frein (exploration)", "Décale le frein pendant l’exploration (0 = neutre).", false, null),
        ("batch_size", "Agent", "Taille de batch", "Plus grand = plus de RAM/CPU pour l’apprentissage.", false, null),
        ("max_episode_py_time", "AssettoCorsa", "Durée d'un épisode (s)", "Chaque voiture roule ce temps (secondes) avant d'être évaluée et de respawn. Plus court = apprentissage plus rapide (plus de candidats par minute) mais moins de données par candidat. 30 s par défaut.", false, null),
    };

    private static readonly HashSet<string> TrackSkipSubdirs = new(StringComparer.OrdinalIgnoreCase)
    {
        "ai", "data", "skins", "ui", "texture", "extension", "sfx",
    };

    public static string[] DiscoverTracks()
    {
        var ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var root in FindAcContentRoots())
            CollectInstalledTracks(Path.Combine(root, "content", "tracks"), ids);

        var gymIds = DiscoverGymTrackIds()
            .Where(id => {
                var (name, layout) = ResolveTrackParts(id);
                return ids.Contains(string.IsNullOrEmpty(layout) ? name : name + "/" + layout);
            })
            .OrderBy(x => x, StringComparer.OrdinalIgnoreCase)
            .ToArray();
        if (gymIds.Length > 0)
            return gymIds;
        if (ids.Contains("monza"))
            return new[] { "monza" };
        return ids.OrderBy(x => x, StringComparer.OrdinalIgnoreCase).DefaultIfEmpty("monza").ToArray();
    }

    public static string[] DiscoverCars()
    {
        var ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var root in FindAcContentRoots())
            CollectInstalledCars(Path.Combine(root, "content", "cars"), ids);

        var cars = ids.Where(id => File.Exists(Path.Combine(CarsDir, id, "steer_map.csv")))
            .OrderBy(x => x, StringComparer.OrdinalIgnoreCase)
            .ToArray();
        return cars.Length > 0 ? cars : new[] { "lotus_exos_125" };
    }

    private static readonly Lazy<string[]> Devices = new(() => {
        try {
            var start = new System.Diagnostics.ProcessStartInfo(RepoPaths.Python) {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            start.ArgumentList.Add("-c");
            start.ArgumentList.Add("import torch; print('cuda' if torch.cuda.is_available() else 'cpu')");
            using var process = System.Diagnostics.Process.Start(start)!;
            var output = process.StandardOutput.ReadToEndAsync();
            var error = process.StandardError.ReadToEndAsync();
            if (!process.WaitForExit(10000)) { process.Kill(); return new[] { "cpu" }; }
            if (process.ExitCode == 0 && output.GetAwaiter().GetResult().Trim() == "cuda")
                return new[] { "cpu", "cuda" };
        } catch { /* CPU remains available if the capability probe fails. */ }
        return new[] { "cpu" };
    });
    public static string[] DiscoverDevices() => Devices.Value;

    public static (string TrackName, string TrackConfig) ResolveTrackParts(string trackId)
    {
        var id = (trackId ?? "").Trim();
        if (string.IsNullOrEmpty(id))
            return ("monza", "");

        // Format jeu : track/layout (ex. ks_silverstone/gp)
        var slash = id.IndexOf('/');
        if (slash > 0 && slash < id.Length - 1)
            return (id[..slash], id[(slash + 1)..]);

        // Format gym : ks_barcelona-layout_gp
        if (File.Exists(TracksConfigPath))
        {
            try
            {
                using var reader = new StreamReader(TracksConfigPath);
                var yaml = new YamlStream();
                yaml.Load(reader);
                if (yaml.Documents.Count > 0 && yaml.Documents[0].RootNode is YamlMappingNode root)
                {
                    var node = TryGetChild(root, id) as YamlMappingNode;
                    if (node is not null)
                    {
                        var name = (TryGetChild(node, "track_name") as YamlScalarNode)?.Value?.Trim();
                        var cfg = (TryGetChild(node, "track_configuration") as YamlScalarNode)?.Value?.Trim() ?? "";
                        if (string.IsNullOrEmpty(name))
                            name = id;
                        return (name, cfg);
                    }
                }
            }
            catch
            {
                // fallback below
            }
        }

        return (id, "");
    }

    private static IEnumerable<string> FindAcContentRoots()
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var list = new List<string>();

        void TryAdd(string? path)
        {
            if (string.IsNullOrWhiteSpace(path))
                return;
            try
            {
                var full = Path.GetFullPath(path.Trim());
                if (!Directory.Exists(Path.Combine(full, "content")))
                    return;
                if (seen.Add(full))
                    list.Add(full);
            }
            catch
            {
                // ignore
            }
        }

        // local-paths.json
        var cfgPath = Path.Combine(RepoPaths.Root, "config", "local-paths.json");
        if (File.Exists(cfgPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(cfgPath));
                if (doc.RootElement.TryGetProperty("aiInstallPath", out var ai) && ai.ValueKind == JsonValueKind.String)
                    TryAdd(ai.GetString());
            }
            catch
            {
                // ignore
            }
        }

        if (list.Count == 0)
            TryAdd(Path.Combine(RepoPaths.Root, "AssettoCorsa-AI", "assettocorsa"));

        return list;
    }

    private static bool IsAcContentPlayable(string folder)
    {
        return Directory.Exists(Path.Combine(folder, "data"))
               || File.Exists(Path.Combine(folder, "data.acd"))
               || File.Exists(Path.Combine(folder, "models.ini"));
    }

    private static void CollectInstalledTracks(string tracksRoot, HashSet<string> ids)
    {
        if (!Directory.Exists(tracksRoot))
            return;

        foreach (var trackDir in Directory.GetDirectories(tracksRoot))
        {
            var trackName = Path.GetFileName(trackDir);
            if (string.IsNullOrWhiteSpace(trackName))
                continue;

            var layouts = new List<string>();
            foreach (var sub in Directory.GetDirectories(trackDir))
            {
                var layout = Path.GetFileName(sub);
                if (string.IsNullOrWhiteSpace(layout) || TrackSkipSubdirs.Contains(layout))
                    continue;
                if (IsAcContentPlayable(sub))
                    layouts.Add(layout);
            }

            var rootOk = IsAcContentPlayable(trackDir);
            if (layouts.Count > 0)
            {
                foreach (var layout in layouts)
                    ids.Add(trackName + "/" + layout);
                // Certaines pistes ont aussi un layout « défaut » à la racine.
                if (rootOk)
                    ids.Add(trackName);
            }
            else if (rootOk)
            {
                ids.Add(trackName);
            }
        }
    }

    private static void CollectInstalledCars(string carsRoot, HashSet<string> ids)
    {
        if (!Directory.Exists(carsRoot))
            return;

        foreach (var carDir in Directory.GetDirectories(carsRoot))
        {
            var name = Path.GetFileName(carDir);
            if (string.IsNullOrWhiteSpace(name))
                continue;
            if (IsAcContentPlayable(carDir))
                ids.Add(name);
        }
    }

    private static IEnumerable<string> DiscoverGymTrackIds()
    {
        if (!File.Exists(TracksConfigPath))
            yield break;

        YamlStream yaml;
        try
        {
            using var reader = new StreamReader(TracksConfigPath);
            yaml = new YamlStream();
            yaml.Load(reader);
        }
        catch
        {
            yield break;
        }

        if (yaml.Documents.Count == 0 || yaml.Documents[0].RootNode is not YamlMappingNode root)
            yield break;

        foreach (var entry in root.Children)
        {
            if (entry.Key is YamlScalarNode sk && !string.IsNullOrWhiteSpace(sk.Value)
                && entry.Value is YamlMappingNode config)
            {
                var files = new[] { "track_file", "ref_lap_file" };
                if (files.All(k => TryGetChild(config, k) is YamlScalarNode file
                    && !string.IsNullOrWhiteSpace(file.Value)
                    && File.Exists(Path.Combine(Path.GetDirectoryName(TracksConfigPath)!, file.Value))))
                    yield return sk.Value!;
            }
        }
    }

    public static List<SettingField> LoadYamlFields()
    {
        var path = RepoPaths.RewardConfig;
        if (!File.Exists(path))
            throw new FileNotFoundException("Config YAML introuvable.", path);

        var text = File.ReadAllText(path);
        var map = new Dictionary<string, string>(StringComparer.Ordinal);
        using (var reader = new StringReader(text))
        {
            var yaml = new YamlStream();
            yaml.Load(reader);
            if (yaml.Documents.Count > 0 && yaml.Documents[0].RootNode is YamlMappingNode root)
            {
                foreach (var (key, section, _, _, isBool, _) in YamlFields)
                {
                    YamlMappingNode? parent = root;
                    if (!string.IsNullOrEmpty(section))
                        parent = TryGetChild(root, section) as YamlMappingNode;
                    if (parent is null)
                        continue;
                    if (TryGetChild(parent, key) is YamlScalarNode scalar && scalar.Value is not null)
                        map[key] = scalar.Value;
                    else if (isBool)
                        map[key] = "False";
                }
            }
        }

        return YamlFields.Select(f =>
        {
            var choices = f.Choices?.Invoke() ?? Array.Empty<string>();
            var value = map.TryGetValue(f.Key, out var v) ? v : (f.IsBool ? "False" : "");
            if (choices.Length > 0 && !string.IsNullOrWhiteSpace(value)
                && !choices.Contains(value, StringComparer.OrdinalIgnoreCase))
            {
                choices = choices.Concat(new[] { value }).OrderBy(x => x, StringComparer.OrdinalIgnoreCase).ToArray();
            }

            return new SettingField
            {
                Key = f.Key,
                Section = f.Section,
                Label = f.Label,
                Hint = f.Hint,
                IsBool = f.IsBool,
                IsChoice = choices.Length > 0,
                Choices = choices,
                Value = value,
            };
        }).ToList();
    }

    private static string BuildYaml(IEnumerable<SettingField> fields)
    {
        var text = File.ReadAllText(RepoPaths.RewardConfig);
        foreach (var field in fields)
            text = ConfigFiles.SetYamlValue(text, field.Section, field.Key, FormatYamlValue(field));
        return text;
    }

    public sealed class UiPrefs
    {
        public int WorkerWidth { get; set; } = 832;
        public int WorkerHeight { get; set; } = 468;
        public int WorkerCount { get; set; } = 2;
        public string QualityPreset { get; set; } = "leger";
        public bool GameSound { get; set; }
        public double GameMasterVolume { get; set; } = 1.0;
        public string TrackId { get; set; } = "monza";
        public string CarId { get; set; } = "lotus_exos_125";
        public string TrackName { get; set; } = "monza";
        public string TrackConfig { get; set; } = "";
    }

    public static UiPrefs LoadUiPrefs()
    {
        var path = RepoPaths.UiSettings;
        if (!File.Exists(path))
            return new UiPrefs();
        try
        {
            var prefs = JsonSerializer.Deserialize<UiPrefs>(File.ReadAllText(path));
            return prefs ?? new UiPrefs();
        }
        catch
        {
            return new UiPrefs();
        }
    }

    public static void SaveAll(IEnumerable<SettingField> fields, UiPrefs prefs)
    {
        var list = fields.ToList();
        ValidatePrefs(prefs);
        foreach (var field in list) FormatYamlValue(field);
        var changes = new Dictionary<string, string> { [RepoPaths.RewardConfig] = BuildYaml(list) };

        var trackId = list.FirstOrDefault(f => f.Key == "track")?.Value?.Trim() ?? "monza";
        var carId = list.FirstOrDefault(f => f.Key == "car")?.Value?.Trim() ?? "lotus_exos_125";
        var (trackName, trackConfig) = ResolveTrackParts(trackId);

        prefs.TrackId = trackId;
        prefs.CarId = carId;
        prefs.TrackName = trackName;
        prefs.TrackConfig = trackConfig ?? "";

        Directory.CreateDirectory(Path.GetDirectoryName(RepoPaths.UiSettings)!);
        var json = JsonSerializer.Serialize(prefs, new JsonSerializerOptions { WriteIndented = true });
        changes[RepoPaths.UiSettings] = json;
        foreach (var raceIni in EnumerateRaceIniPaths()) {
            if (!File.Exists(raceIni)) throw new FileNotFoundException("Profil worker absent", raceIni);
            var text = File.ReadAllText(raceIni);
            text = SetIniValue(text, "RACE", "MODEL", carId);
            text = SetIniValue(text, "RACE", "TRACK", trackName);
            changes[raceIni] = SetIniValue(text, "RACE", "CONFIG_TRACK", prefs.TrackConfig);
        }
        ApplyWorkerVideo(prefs, changes);
        ApplyGameAudio(prefs, changes);
        ConfigFiles.WriteAll(changes);
    }

    public static void SaveUiPrefsOnly(UiPrefs prefs)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(RepoPaths.UiSettings)!);
        var json = JsonSerializer.Serialize(prefs, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(RepoPaths.UiSettings, json);
    }

    public static void ApplyQualityPreset(UiPrefs prefs, string preset)
    {
        prefs.QualityPreset = preset;
        switch (preset.ToLowerInvariant())
        {
            case "minimal":
                prefs.WorkerWidth = 640;
                prefs.WorkerHeight = 360;
                break;
            case "leger":
                prefs.WorkerWidth = 832;
                prefs.WorkerHeight = 468;
                break;
            case "moyen":
                prefs.WorkerWidth = 1280;
                prefs.WorkerHeight = 720;
                break;
            case "eleve":
                prefs.WorkerWidth = 1600;
                prefs.WorkerHeight = 900;
                break;
        }
    }

    private static IEnumerable<string> EnumerateRaceIniPaths()
    {
        yield return Path.Combine(RepoPaths.Root, "ac_profiles", "ai", "cfg", "race.ini");
        var workersRoot = Path.Combine(RepoPaths.Root, "ac_profiles", "workers");
        if (!Directory.Exists(workersRoot))
            yield break;
        foreach (var dir in Directory.GetDirectories(workersRoot, "worker_*"))
            yield return Path.Combine(dir, "cfg", "race.ini");
    }

    private static readonly (string Section, string Key, string Value)[] LowGraphicsReductions =
    {
        ("POST_PROCESS", "ENABLED", "0"),
        ("POST_PROCESS", "QUALITY", "0"),
        ("POST_PROCESS", "GLARE", "0"),
        ("POST_PROCESS", "DOF", "0"),
        ("POST_PROCESS", "HEAT_SHIMMER", "0"),
        ("POST_PROCESS", "RAYS_OF_GOD", "0"),
        ("EFFECTS", "SMOKE", "0"),
        ("EFFECTS", "RENDER_SMOKE_IN_MIRROR", "0"),
        ("MIRROR", "HQ", "0"),
        ("MIRROR", "SIZE", "256"),
        ("VIDEO", "SHADOW_MAP_SIZE", "256"),
        ("VIDEO", "AASAMPLES", "0"),
        ("VIDEO", "AAQUALITY", "0"),
        ("VIDEO", "ANISOTROPIC", "0"),
        ("VIDEO", "VSYNC", "0"),
        ("ASSETTOCORSA", "WORLD_DETAIL", "0"),
        ("CUBEMAP", "SIZE", "0"),
        ("CUBEMAP", "FACES_PER_FRAME", "0"),
    };

    private static void ApplyWorkerVideo(UiPrefs prefs, Dictionary<string, string> changes)
    {
        var w = Math.Clamp(prefs.WorkerWidth, 320, 3840);
        var h = Math.Clamp(prefs.WorkerHeight, 240, 2160);
        foreach (var video in EnumerateVideoIniPaths())
        {
            if (!File.Exists(video))
                throw new FileNotFoundException("Profil vidéo absent", video);
            var text = File.ReadAllText(video);
            text = SetIniValue(text, "VIDEO", "WIDTH", w.ToString(CultureInfo.InvariantCulture));
            text = SetIniValue(text, "VIDEO", "HEIGHT", h.ToString(CultureInfo.InvariantCulture));
            text = SetIniValue(text, "VIDEO", "FULLSCREEN", "0");
            foreach (var r in LowGraphicsReductions)
                text = SetIniValue(text, r.Section, r.Key, r.Value);
            changes[video] = text;
        }
    }

    private static IEnumerable<string> EnumerateVideoIniPaths()
    {
        yield return Path.Combine(RepoPaths.Root, "ac_profiles", "ai", "cfg", "video.ini");
        var workersRoot = Path.Combine(RepoPaths.Root, "ac_profiles", "workers");
        if (!Directory.Exists(workersRoot))
            yield break;
        foreach (var dir in Directory.GetDirectories(workersRoot, "worker_*"))
            yield return Path.Combine(dir, "cfg", "video.ini");
    }

    private static void ApplyGameAudio(UiPrefs prefs, Dictionary<string, string> changes)
    {
        var vol = prefs.GameSound
            ? Math.Clamp(prefs.GameMasterVolume, 0.0, 1.0).ToString("0.###", CultureInfo.InvariantCulture)
            : "0";
        foreach (var audio in FindAudioIniCandidates())
        {
            var text = File.Exists(audio) ? File.ReadAllText(audio) : "";
            text = SetIniValue(text, "LEVELS", "MASTER", vol);
            changes[audio] = text;
        }
    }

    private static IEnumerable<string> FindAudioIniCandidates()
    {
        yield return Path.Combine(RepoPaths.Root, "ac_profiles", "ai", "cfg", "audio.ini");
        var workersRoot = Path.Combine(RepoPaths.Root, "ac_profiles", "workers");
        if (Directory.Exists(workersRoot))
        {
            foreach (var dir in Directory.GetDirectories(workersRoot, "worker_*"))
                yield return Path.Combine(dir, "cfg", "audio.ini");
        }

        // Live Documents (copied again at worker launch from profile, but keep in sync on Save).
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        yield return Path.Combine(home, "OneDrive", "Documents", "Assetto Corsa", "cfg", "audio.ini");
        yield return Path.Combine(home, "Documents", "Assetto Corsa", "cfg", "audio.ini");
    }

    private static string SetIniValue(string text, string section, string key, string value)
    {
        var sectionRx = new Regex(
            @"(\[" + Regex.Escape(section) + @"\][^\[]*)",
            RegexOptions.IgnoreCase | RegexOptions.Singleline);
        if (!sectionRx.IsMatch(text))
            return text + "\r\n[" + section + "]\r\n" + key + "=" + value + "\r\n";

        return sectionRx.Replace(text, m =>
        {
            var block = m.Groups[1].Value;
            var keyRx = new Regex(@"(?m)^(" + Regex.Escape(key) + @")\s*=.*$");
            if (keyRx.IsMatch(block))
                return keyRx.Replace(block, key + "=" + value, 1);
            return block.TrimEnd() + "\r\n" + key + "=" + value + "\r\n";
        }, 1);
    }

    private static string FormatYamlValue(SettingField field)
    {
        var definition = YamlFields.FirstOrDefault(f => f.Key == field.Key && f.Section == field.Section);
        if (definition.Key is null)
            throw new ArgumentException("Paramètre non pris en charge : " + field.Key);
        if (definition.Choices is not null && !definition.Choices().Contains(field.Value.Trim(), StringComparer.Ordinal))
            throw new ArgumentException("Valeur indisponible pour " + field.Label + " : " + field.Value);
        if (field.IsBool)
        {
            return field.BoolValue ? "True" : "False";
        }

        if (field.Key is "track" or "car" or "device" or "color_mode")
        {
            var s = field.Value.Trim();
            return s.Contains(' ') || s.Contains(':') ? "\"" + s + "\"" : s;
        }

        if (double.TryParse(field.Value.Trim().Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out var d))
        {
            if (!double.IsFinite(d) || (field.Key == "batch_size" && (d < 1 || d > 50000 || d != Math.Floor(d))) || (field.Key == "action_exploration_noise_std" && (d < 0 || d > 1)) || (field.Key.EndsWith("_bias") && Math.Abs(d) > 1))
                throw new ArgumentException("Valeur hors limites : " + field.Label);
            if (Math.Abs(d - Math.Round(d)) < 1e-12 && Math.Abs(d) < 1e12)
                return ((long)Math.Round(d)).ToString(CultureInfo.InvariantCulture);
            return d.ToString("0.########", CultureInfo.InvariantCulture);
        }

        throw new ArgumentException("Nombre invalide : " + field.Label);
    }

    private static void ValidatePrefs(UiPrefs prefs)
    {
        if (prefs.WorkerWidth < 320 || prefs.WorkerWidth > 3840 || prefs.WorkerHeight < 240 || prefs.WorkerHeight > 2160)
            throw new ArgumentException("Résolution : largeur de 320 à 3840, hauteur de 240 à 2160.");
        if (!double.IsFinite(prefs.GameMasterVolume) || prefs.GameMasterVolume < 0 || prefs.GameMasterVolume > 1)
            throw new ArgumentException("Le volume doit être un nombre entre 0 et 1.");
        if (prefs.WorkerCount < 1 || prefs.WorkerCount > 6)
            throw new ArgumentException("Nombre de workers : de 1 à 6.");
    }

    private static YamlNode? TryGetChild(YamlMappingNode map, string key)
    {
        foreach (var entry in map.Children)
        {
            if (entry.Key is YamlScalarNode sk && sk.Value == key)
                return entry.Value;
        }
        return null;
    }

}
