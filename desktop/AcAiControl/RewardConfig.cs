using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using YamlDotNet.RepresentationModel;

namespace AcAiControl;

internal sealed class RewardField
{
    public string Key { get; init; } = "";
    public string Label { get; init; } = "";
    public string Value { get; set; } = "";
    public bool IsBool { get; init; }
}

internal static class RewardConfig
{
    public static readonly (string Key, string Label, bool IsBool)[] Fields =
    {
        ("reward_progress_per_m", "Progression / m", false),
        ("reward_time_per_sec", "Coût du temps / s", false),
        ("reward_collision_penalty", "Pénalité collision / sortie", false),
        ("reward_clean_lap_bonus", "Bonus tour propre", false),
    };

    public static List<RewardField> Load()
    {
        var path = RepoPaths.RewardConfig;
        if (!File.Exists(path))
            throw new FileNotFoundException("Config YAML introuvable.", path);

        var text = File.ReadAllText(path);
        EnsureSupportedMode(text);
        var map = new Dictionary<string, string>(StringComparer.Ordinal);
        using (var reader = new StringReader(text))
        {
            var yaml = new YamlStream();
            yaml.Load(reader);
            if (yaml.Documents.Count > 0 &&
                yaml.Documents[0].RootNode is YamlMappingNode root &&
                TryGetChild(root, "AssettoCorsa") is YamlMappingNode ac)
            {
                foreach (var (key, _, isBool) in Fields)
                {
                    if (TryGetChild(ac, key) is YamlScalarNode scalar && scalar.Value is not null)
                        map[key] = scalar.Value;
                    else if (isBool)
                        map[key] = "False";
                }
            }
        }

        return Fields.Select(f => new RewardField
        {
            Key = f.Key,
            Label = f.Label,
            IsBool = f.IsBool,
            Value = map.TryGetValue(f.Key, out var v) ? v : (f.IsBool ? "False" : "0"),
        }).ToList();
    }

    public static void Save(IEnumerable<RewardField> fields)
    {
        var path = RepoPaths.RewardConfig;
        var original = File.ReadAllText(path);
        EnsureSupportedMode(original);
        var updated = original;
        foreach (var field in fields)
            updated = ConfigFiles.SetYamlValue(updated, "AssettoCorsa", field.Key, FormatYamlValue(field));
        ConfigFiles.WriteAll(new Dictionary<string, string> { [path] = updated });
    }

    private static string FormatYamlValue(RewardField field)
    {
        if (!Fields.Any(f => f.Key == field.Key))
            throw new ArgumentException("Récompense non prise en charge : " + field.Key);

        if (double.TryParse(field.Value.Trim().Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out var d))
        {
            if (!double.IsFinite(d) || d < 0 || d > 1000000 || (field.Key == "reward_n_sectors" && (d < 2 || d > 100 || d != Math.Floor(d))) || (field.Key == "reward_clean_lap_time_ref_sec" && d <= 0))
                throw new ArgumentException("Valeur hors limites : " + field.Label);
            if (Math.Abs(d - Math.Round(d)) < 1e-12 && Math.Abs(d) < 1e12)
                return ((long)Math.Round(d)).ToString(CultureInfo.InvariantCulture);
            return d.ToString("0.########", CultureInfo.InvariantCulture);
        }

        throw new ArgumentException("Nombre invalide : " + field.Label);
    }

    private static void EnsureSupportedMode(string text)
    {
        var yaml = new YamlStream();
        yaml.Load(new StringReader(text));
        if (yaml.Documents.Count == 0 || yaml.Documents[0].RootNode is not YamlMappingNode root
            || TryGetChild(root, "AssettoCorsa") is not YamlMappingNode ac
            || TryGetChild(ac, "reward_mode") is not YamlScalarNode mode || mode.Value != "signed_progress_v1")
            throw new InvalidOperationException("Cet éditeur nécessite le mode de récompense signed_progress_v1.");
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
