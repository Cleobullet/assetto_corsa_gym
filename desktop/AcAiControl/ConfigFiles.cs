using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace AcAiControl;

internal static class ConfigFiles
{
    // Edit only the requested YAML section while preserving comments and unrelated settings.
    public static string SetYamlValue(string text, string section, string key, string value)
    {
        if (section.Length == 0) {
            var rootKey = new Regex(@"(?m)^" + Regex.Escape(key) + @"[ \t]*:[^\r\n]*");
            return rootKey.IsMatch(text) ? rootKey.Replace(text, _ => key + ": " + value, 1)
                : key + ": " + value + "\n" + text;
        }
        var blocks = Regex.Matches(text, @"(?m)^[A-Za-z_][A-Za-z0-9_-]*:");
        var header = blocks.Cast<Match>().FirstOrDefault(m => m.Value == section + ":")
            ?? throw new InvalidOperationException("Section YAML absente : " + section);
        var end = blocks.Cast<Match>().FirstOrDefault(m => m.Index > header.Index)?.Index ?? text.Length;
        var block = text[header.Index..end];
        var field = new Regex(@"(?m)^([ \t]+)" + Regex.Escape(key) + @"[ \t]*:[^\r\n]*");
        if (field.IsMatch(block))
            block = field.Replace(block, m => m.Groups[1].Value + key + ": " + value, 1);
        else {
            var newline = block.IndexOf('\n');
            if (newline < 0) block += "\n  " + key + ": " + value + "\n";
            else block = block.Insert(newline + 1, "  " + key + ": " + value + "\n");
        }
        return text[..header.Index] + block + text[end..];
    }

    // Prepare every output before changing files; restore committed files if a later write fails.
    public static void WriteAll(IReadOnlyDictionary<string, string> files)
    {
        var originals = files.Keys.ToDictionary(p => p, p => File.Exists(p) ? File.ReadAllBytes(p) : null);
        var staged = new Dictionary<string, string>();
        var committed = new List<string>();
        try {
            foreach (var (path, content) in files) {
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                var tmp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
                staged[path] = tmp;
                File.WriteAllText(tmp, content, new UTF8Encoding(false));
            }
            foreach (var (path, tmp) in staged) {
                File.Move(tmp, path, overwrite: true);
                committed.Add(path);
            }
        } catch (Exception error) {
            var errors = new List<Exception> { error };
            foreach (var path in committed.AsEnumerable().Reverse()) {
                try {
                    if (originals[path] is { } bytes) File.WriteAllBytes(path, bytes);
                    else File.Delete(path);
                } catch (Exception rollbackError) { errors.Add(rollbackError); }
            }
            if (errors.Count > 1) throw new AggregateException("Enregistrement incomplet ; restauration partielle des paramètres.", errors);
            throw;
        } finally {
            foreach (var tmp in staged.Values) if (File.Exists(tmp)) File.Delete(tmp);
        }
    }
}
