using System.Globalization;
using AcAiControl;

// Exercise the production settings writers against an isolated project, never the live files.
var root = RepoPaths.Root;
void Write(string path, string contents) {
    var target = Path.Combine(root, path);
    Directory.CreateDirectory(Path.GetDirectoryName(target)!);
    File.WriteAllText(target, contents);
}
void Check(bool condition, string message) {
    if (!condition) throw new Exception(message);
}
void Refuses(Action action, string message) {
    var before = File.ReadAllText(RepoPaths.RewardConfig);
    try { action(); } catch (ArgumentException) {
        Check(before == File.ReadAllText(RepoPaths.RewardConfig), "Invalid input changed the YAML");
        return;
    }
    throw new Exception(message);
}
Write("AssettoCorsa-AI/assettocorsa/content/tracks/circuit/layout/data/test", "");
Write("AssettoCorsa-AI/assettocorsa/content/tracks/unsupported/data/test", "");
Write("AssettoCorsa-AI/assettocorsa/content/cars/car/data/test", "");
Write("AssettoCorsa-AI/assettocorsa/content/cars/unsupported/data/test", "");
Write("assetto_corsa_gym/AssettoCorsaConfigs/cars/car/steer_map.csv", "x,y\n0,0\n1,35\n");
Write("assetto_corsa_gym/AssettoCorsaConfigs/tracks/track.csv", "");
Write("assetto_corsa_gym/AssettoCorsaConfigs/tracks/line.csv", "");
Write("assetto_corsa_gym/AssettoCorsaConfigs/tracks/config.yaml", "circuit-layout:\n  track_name: circuit\n  track_configuration: layout\n  track_file: track.csv\n  ref_lap_file: line.csv\nmissing:\n  track_file: missing.csv\n  ref_lap_file: line.csv\n");
foreach (var profile in new[] { "ai", "workers/worker_0", "workers/worker_1" }) {
    Write($"ac_profiles/{profile}/cfg/race.ini", "[RACE]\nMODEL=old\nTRACK=old\nCONFIG_TRACK=\n");
    Write($"ac_profiles/{profile}/cfg/video.ini", "[VIDEO]\nWIDTH=832\nHEIGHT=468\nFULLSCREEN=0\n");
}
Write("config_first_sac_cpu_v3.yml", "device: cpu\nAgent:\n  batch_size: 64\n  action_exploration_noise_std: 0\n  action_exploration_throttle_bias: 0\n  action_exploration_brake_bias: 0\nAssettoCorsa:\n  track: circuit-layout\n  car: car\n  reward_mode: signed_progress_v1\n");

Check(SettingsConfig.DiscoverTracks().SequenceEqual(new[] { "circuit-layout" }), "Unsupported track offered");
Check(SettingsConfig.DiscoverCars().SequenceEqual(new[] { "car" }), "Uncalibrated car offered");
var fields = SettingsConfig.LoadYamlFields();
fields.Single(f => f.Key == "batch_size").Value = "128";
fields.Single(f => f.Key == "action_exploration_noise_std").Value = "0,1";
fields.Single(f => f.Key == "action_exploration_throttle_bias").Value = "0.2";
fields.Single(f => f.Key == "action_exploration_brake_bias").Value = "-0.1";
var prefs = new SettingsConfig.UiPrefs { WorkerWidth = 1280, WorkerHeight = 720, GameSound = true, GameMasterVolume = .4 };
SettingsConfig.SaveAll(fields, prefs);
Check(prefs.TrackName == "circuit" && prefs.TrackConfig == "layout", "Track layout lost");
foreach (var profile in new[] { "ai", "workers/worker_0", "workers/worker_1" }) {
    var cfg = Path.Combine(root, "ac_profiles", profile, "cfg");
    var race = File.ReadAllText(Path.Combine(cfg, "race.ini"));
    Check(race.Contains("MODEL=car") && race.Contains("TRACK=circuit") && race.Contains("CONFIG_TRACK=layout"), "Race fields not applied");
    var video = File.ReadAllText(Path.Combine(cfg, "video.ini"));
    Check(video.Contains("WIDTH=1280") && video.Contains("HEIGHT=720"), "Resolution not applied");
    Check(File.ReadAllText(Path.Combine(cfg, "audio.ini")).Contains("MASTER=0.4"), "Audio not created/applied");
}
Check(SettingsConfig.LoadYamlFields().Single(f => f.Key == "batch_size").Value == "128", "Batch not persisted");
prefs.GameSound = false;
SettingsConfig.SaveAll(fields, prefs);
Check(File.ReadAllText(Path.Combine(root, "ac_profiles/ai/cfg/audio.ini")).Contains("MASTER=0"), "Mute not applied");
foreach (var (key, invalid) in new[] { ("batch_size", "1.5"), ("batch_size", "NaN"), ("action_exploration_noise_std", "-1"), ("action_exploration_brake_bias", "2"), ("car", "unsupported"), ("track", "unsupported"), ("device", "invalid") }) {
    var field = fields.Single(f => f.Key == key); var old = field.Value;
    field.Value = invalid;
    Refuses(() => SettingsConfig.SaveAll(fields, prefs), "Invalid setting accepted: " + key);
    field.Value = old;
}
prefs.WorkerWidth = 10;
Refuses(() => SettingsConfig.SaveAll(fields, prefs), "Invalid resolution accepted");
prefs.WorkerWidth = 1280; prefs.GameMasterVolume = double.NaN;
Refuses(() => SettingsConfig.SaveAll(fields, prefs), "NaN volume accepted");

var rewards = RewardConfig.Load();
foreach (var field in rewards) field.Value = field.Key == "reward_n_sectors" ? "4" : "0,25";
RewardConfig.Save(rewards);
Check(RewardConfig.Load().All(f => f.Value == (f.Key == "reward_n_sectors" ? "4" : "0.25")), "Reward roundtrip failed");
foreach (var field in rewards) {
    var old = field.Value; field.Value = "NaN";
    Refuses(() => RewardConfig.Save(rewards), "NaN reward accepted: " + field.Key);
    field.Value = old;
}
rewards.Single(f => f.Key == "reward_n_sectors").Value = "1";
Refuses(() => RewardConfig.Save(rewards), "Unsupported sector count accepted");
var scoped = "device: cpu\nOther:\n  batch_size: 7\n  reward_progress_per_m: 9\nAgent:\n  batch_size: 64\nAssettoCorsa:\n  reward_progress_per_m: 0.1\n";
scoped = ConfigFiles.SetYamlValue(scoped, "Agent", "batch_size", "128");
scoped = ConfigFiles.SetYamlValue(scoped, "AssettoCorsa", "reward_progress_per_m", "0.2");
Check(scoped.Contains("Other:\n  batch_size: 7\n  reward_progress_per_m: 9") && scoped.Contains("Agent:\n  batch_size: 128"), "Editing crossed YAML sections");
var beforeFailure = File.ReadAllText(RepoPaths.RewardConfig);
var badTarget = Path.Combine(root, "a-directory");
Directory.CreateDirectory(badTarget);
try {
    ConfigFiles.WriteAll(new Dictionary<string, string> { [RepoPaths.RewardConfig] = "changed", [badTarget] = "cannot replace a directory" });
    throw new Exception("I/O error was ignored");
} catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) {
    Check(File.ReadAllText(RepoPaths.RewardConfig) == beforeFailure, "Partial save not rolled back");
}
Console.WriteLine("PASS: compatible content, layout mapping, all setting writes, reward roundtrip, invalid input, profiles, audio, resolution.");
Console.WriteLine("Isolated fixture: " + root);

namespace AcAiControl {
    internal static class RepoPaths {
        public static string Root { get; } = Path.Combine(Path.GetTempPath(), "AcAiSettingsTests-" + Guid.NewGuid().ToString("N"));
        public static string RewardConfig => Path.Combine(Root, "config_first_sac_cpu_v3.yml");
        public static string UiSettings => Path.Combine(Root, "config/ui_settings.json");
        public static string Python => Path.Combine(Root, "no-python.exe");
    }
}
