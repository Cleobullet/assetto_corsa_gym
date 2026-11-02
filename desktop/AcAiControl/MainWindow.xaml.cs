using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace AcAiControl;

public partial class MainWindow : Window
{
    private readonly ObservableCollection<WorkerSnapshot> _workers = new();
    private readonly ObservableCollection<RewardField> _rewards = new();
    private readonly ObservableCollection<SettingField> _settings = new();
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private C4dClient? _client;
    private bool _busy;
    private bool _settingsPresetSilent;
    private CancellationTokenSource? _opCts;

    private readonly Dictionary<int, List<DiagSample>> _diagSamples = new();
    private readonly Dictionary<int, long> _diagLastTransitions = new();
    private readonly Dictionary<int, long> _diagLastTelemetryFrames = new();
    private readonly Dictionary<int, double> _speedEma = new();
    private DateTime _diagLastSampleAt = DateTime.MinValue;

    private sealed class DiagSample
    {
        public double StepsPer2s { get; init; }
        public double CpuPercent { get; init; }
        public double SpeedKmh { get; init; }
        public double TelemetryAgeMs { get; init; }
        public int EpisodeCount { get; init; }
        public double Reward { get; init; }
        public double GameFps { get; init; }
        public double TrainHz { get; init; }
        public int NeuroCandidate { get; init; } = -1;
        public int NeuroLaps { get; init; }
        public double NeuroProgressM { get; init; }
    }

    private readonly List<(string Label, string Pattern)> _logOptions = new();
    private string _currentLogPath = "";
    private DateTime _lastLogRefreshAt = DateTime.MinValue;

    public MainWindow()
    {
        InitializeComponent();
        VersionLabel.Text = RepoPaths.Version;
        for (var i = 0; i < 6; i++)
            _workers.Add(new WorkerSnapshot { Id = i });
        WorkerGrid.ItemsSource = _workers;
        RewardList.ItemsSource = _rewards;
        SettingsList.ItemsSource = _settings;
        ReloadSaves();
        _timer.Tick += (_, _) => RefreshUi();
        PopulateLogPicker();
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        try
        {
            AppendLog("Racine : " + RepoPaths.Root);
            SetBusy(true, "Connexion…");
            var (client, _) = await ServerBootstrap.EnsureConnectedAsync(AppendLog);
            _client = client;
            ConnectionLabel.Text = "Connecté au centre de contrôle";
            ReloadRewards();
            ReloadSettings();
            RefreshUi();
            _timer.Start();
            SetStatus("Prêt.");
        }
        catch (Exception ex)
        {
            ConnectionLabel.Text = "Hors ligne";
            AppendLog("Erreur de connexion : " + ex.Message);
            MessageBox.Show(this, ex.Message, "Connexion", MessageBoxButton.OK, MessageBoxImage.Error);
            SetStatus("Connexion échouée.");
        }
        finally
        {
            SetBusy(false);
        }
    }

    private void Window_Closing(object? sender, CancelEventArgs e)
    {
        _timer.Stop();
        _opCts?.Cancel();
        _client?.Dispose();
        // Closing the window must not stop the game or learner.
    }

    private void RefreshUi()
    {
        try
        {
            for (var i = 0; i < 6; i++)
                _workers[i] = ProcessWatch.ReadWorker(i);
            WorkerGrid.Items.Refresh();

            var learner = ProcessWatch.ReadLearner();
            LearnerLabel.Text =
                $"Apprentissage : {(learner.Alive ? "actif" : "arrêté")} • {learner.GlobalSteps} étapes • {learner.Updates} mises à jour • relevé âgé de {learner.AgeSec:0} s";

            RenderDiagnostic();

            // Auto-refresh des logs (toutes les 2 s)
            if (LogsAutoRefresh.IsChecked == true && (DateTime.UtcNow - _lastLogRefreshAt).TotalSeconds >= 2.0)
            {
                _lastLogRefreshAt = DateTime.UtcNow;
                RefreshLogView();
            }


        }
        catch (Exception ex)
        {
            AppendLog("Refresh: " + ex.Message);
        }
    }

    private async void BtnStartWorkers_Click(object sender, RoutedEventArgs e)
    {
        var n = WorkerCount();
        var range = n == 1 ? "0" : "0-" + (n - 1);
        await RunOpAsync($"Démarrage des {n} workers", async (ct, progress) =>
        {
            var reply = await SendAsync($"scale-test {range}", ct);
            AppendLog(reply);
            var startedAt = DateTime.UtcNow;
            await ProcessWatch.WaitUntilAsync(
                () =>
                {
                    var alive = ProcessWatch.CountAliveWorkers(n);
                    progress($"Démarrage… {alive}/{n} workers vivants");
                    if (alive < n) return false;
                    for (var i = 0; i < n; i++)
                    {
                        var w = ProcessWatch.ReadWorker(i);
                        if (!w.CollectorAlive || w.Phase is "absent" or "")
                            return false;
                    }
                    return true;
                },
                TimeSpan.FromMinutes(12),
                progress,
                ct,
                () => ProcessWatch.LatestSessionFatal(startedAt.AddSeconds(-5)));
        });
    }

    private async void BtnTest_Click(object sender, RoutedEventArgs e)
    {
        var n = WorkerCount();
        var range = n == 1 ? "0" : "0-" + (n - 1);
        await RunOpAsync($"Calibration des {n} voitures", async (ct, progress) =>
        {
            progress("Lancement des voitures…");
            var reply = await SendAsync($"scale-test {range}", ct);
            AppendLog(reply);
            var startedAt = DateTime.UtcNow;
            await ProcessWatch.WaitUntilAsync(
                () =>
                {
                    var alive = ProcessWatch.CountAliveWorkers(n);
                    progress($"Lancement… {alive}/{n} workers");
                    if (alive < n) return false;
                    for (var i = 0; i < n; i++)
                    {
                        var w = ProcessWatch.ReadWorker(i);
                        if (!w.CollectorAlive || w.Phase != "idle")
                            return false;
                    }
                    return true;
                },
                TimeSpan.FromMinutes(12),
                progress,
                ct,
                () => ProcessWatch.LatestSessionFatal(startedAt.AddSeconds(-5)));

            // Le test entre en mode calibrage (le collecteur ne pilote plus et
            // ne réécrit plus le neutre) : la voiture répond aux commandes du
            // test SANS qu'aucun entraînement ne tourne en arrière-plan.
            progress("Calibration vJoy en cours…");
            var cal = await SendAsync("calibrate", ct);
            AppendLog(cal);
            StatusLabel.Text = "Calibration terminée — voir le journal.";
        });
    }

    private async void BtnStopWorkers_Click(object sender, RoutedEventArgs e)
        => await RunOpAsync("Arrêt des workers", async (ct, progress) =>
        {
            var reply = await SendAsync("stop-all", ct);
            AppendLog(reply);
            await ProcessWatch.WaitUntilAsync(
                () =>
                {
                    var alive = ProcessWatch.CountAliveWorkers();
                    progress($"Arrêt… {alive} worker(s) encore actif(s)");
                    return alive == 0;
                },
                TimeSpan.FromMinutes(4),
                progress,
                ct);
        });

    private async void BtnStartLearner_Click(object sender, RoutedEventArgs e)
        => await RunOpAsync("Démarrage apprentissage", async (ct, progress) =>
        {
            var reply = await SendAsync("evolve start", ct);
            AppendLog(reply);
            if (reply.Contains("refus", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(reply);
            // La neuroévolution tourne en arrière-plan (EvolutionRunner) ;
            // l'onglet « Évolution » montre la progression. Il n'y a plus de
            // learner SAC à attendre (l'ancien « learner start » était refusé
            // car les workers ne chargent plus de politique SAC).
        });

    private async void BtnSaveLearner_Click(object sender, RoutedEventArgs e)
    {
        var name = SaveName.Text.Trim();
        if (string.IsNullOrEmpty(name)) name = "sauvegarde-" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
        SaveName.Text = name;
        await SaveSnapshotAsync(name);
    }

    private async Task SaveSnapshotAsync(string name)
    {
        if (!System.Text.RegularExpressions.Regex.IsMatch(name, @"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")) { MessageBox.Show("Nom requis : lettres, chiffres, tirets, points."); return; }
        var folder = Path.Combine(RepoPaths.Root, "outputs", "training_sessions", name);
        if (Directory.Exists(folder)) { MessageBox.Show("Ce nom existe déjà. Choisis un nouveau nom pour conserver la sauvegarde précédente."); return; }
        await RunOpAsync("Sauvegarde " + name, async (ct, progress) =>
        {
            var checkpoint = Path.Combine(RepoPaths.Root, "outputs", "multi_sac_cpu_v3", "trainer_state.json");
            if (ProcessWatch.ReadLearner().Alive)
            {
                var before = File.Exists(checkpoint) ? File.GetLastWriteTimeUtc(checkpoint) : DateTime.MinValue;
                var reply = await SendAsync("learner save", ct);
                AppendLog(reply);
                if (!reply.Contains("learner save requested") || reply.Contains("no active")) throw new InvalidOperationException(reply);
                var request = Path.Combine(RepoPaths.Root, "outputs", "multi_sac_cpu_v3", "learner_save.request");
                await ProcessWatch.WaitUntilAsync(
                    () => !File.Exists(request) && File.Exists(checkpoint) && File.GetLastWriteTimeUtc(checkpoint) > before,
                    TimeSpan.FromMinutes(2), progress, ct);
            }
            var result = await SendAsync("train save " + name, ct);
            AppendLog(result);
            if (!result.StartsWith("session sauvee:")) throw new InvalidOperationException(result);
            if (!File.Exists(Path.Combine(folder, "session.json"))) throw new IOException("La sauvegarde n’a pas été créée.");
            ReloadSaves();
            progress("Copie nommée enregistrée : " + name);
        });
    }

    private async void BtnStopLearner_Click(object sender, RoutedEventArgs e)
        => await RunOpAsync("Arrêt apprentissage", async (ct, progress) =>
        {
            var reply = await SendAsync("evolve stop", ct);
            AppendLog(reply);
            // La neuroévolution s'arrête à la fin du cycle en cours (il n'y a plus
            // de learner SAC à attendre — l'ancien « learner stop » ne faisait rien).
        });

    private int _opGeneration;

    private async void BtnEmergency_Click(object sender, RoutedEventArgs e)
    {
        if (MessageBox.Show(this,
                "Lancer l’arrêt d’urgence ?\n\nCela coupe l’IA, ferme les fenêtres workers, et ferme aussi Assetto Corsa normal si Steam l’a ouvert.",
                "Confirmation",
                MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes)
            return;

        var generation = ++_opGeneration;
        try { _opCts?.Cancel(); } catch { /* l’opération en cours s’arrête */ }
        _opCts = new CancellationTokenSource();
        var ct = _opCts.Token;
        SetBusy(true, "Arrêt d’urgence…");
        AppendLog("Arrêt d’urgence lancé.");
        try
        {
            await Task.Run(() => ProcessWatch.RunEmergencyStop(msg => Dispatcher.Invoke(() => AppendLog(msg))), ct);
            if (generation != _opGeneration) return;
            SetStatus("Arrêt d’urgence : terminé.");
            AppendLog("Arrêt d’urgence : terminé.");
            RefreshUi();
        }
        catch (OperationCanceledException)
        {
            if (generation != _opGeneration) return;
            SetStatus("Arrêt d’urgence : annulé.");
            AppendLog("Arrêt d’urgence : annulé.");
        }
        catch (Exception ex)
        {
            if (generation != _opGeneration) return;
            SetStatus("Arrêt d’urgence : échoué.");
            AppendLog("Arrêt d’urgence : " + ex.Message);
            MessageBox.Show(this, ex.Message, "Arrêt d’urgence", MessageBoxButton.OK, MessageBoxImage.Error);
        }
        finally
        {
            if (generation == _opGeneration)
            {
                SetBusy(false);
                _opCts = null;
            }
        }
    }

    private int SelectedWorkerId()
    {
        return WorkerPicker.SelectedIndex < 0 ? 0 : WorkerPicker.SelectedIndex;
    }

    private int WorkerCount()
    {
        var c = SettingsConfig.LoadUiPrefs().WorkerCount;
        return c is >= 1 and <= 6 ? c : 2;
    }

    private void UpdateStartWorkersLabel()
        => BtnStartWorkers.Content = $"Démarrer les {WorkerCount()} workers";

    private async void BtnWorkerStart_Click(object sender, RoutedEventArgs e)
    {
        var id = SelectedWorkerId();
        await RunOpAsync($"Démarrage voiture {id + 1}", async (ct, progress) =>
        {
            var reply = await SendAsync($"scale-test {id}", ct);
            AppendLog(reply);
            await ProcessWatch.WaitUntilAsync(
                () =>
                {
                    var w = ProcessWatch.ReadWorker(id);
                    progress($"Voiture {id + 1} : {w.Phase}");
                    return w.CollectorAlive && w.Phase is not "absent" and not "";
                },
                TimeSpan.FromMinutes(8),
                progress,
                ct);
        });
    }

    private async void BtnWorkerStop_Click(object sender, RoutedEventArgs e)
    {
        var id = SelectedWorkerId();
        await RunOpAsync($"Arrêt voiture {id + 1}", async (ct, progress) =>
        {
            var reply = await SendAsync($"stop {id}", ct);
            AppendLog(reply);
            await ProcessWatch.WaitUntilAsync(
                () => !ProcessWatch.ReadWorker(id).CollectorAlive,
                TimeSpan.FromMinutes(3),
                progress,
                ct);
        });
    }

    private async void BtnWorkerRestart_Click(object sender, RoutedEventArgs e)
    {
        var id = SelectedWorkerId();
        await RunOpAsync($"Redémarrage voiture {id + 1}", async (ct, progress) =>
        {
            var reply = await SendAsync($"restart {id}", ct);
            AppendLog(reply);
            await ProcessWatch.WaitUntilAsync(
                () =>
                {
                    var w = ProcessWatch.ReadWorker(id);
                    progress($"Voiture {id + 1} : {w.Phase}");
                    return w.CollectorAlive && w.Phase is not "absent" and not "";
                },
                TimeSpan.FromMinutes(10),
                progress,
                ct);
        });
    }

    private async void BtnWorkerDiag_Click(object sender, RoutedEventArgs e)
    {
        var id = SelectedWorkerId();
        await RunOpAsync($"Diagnostic voiture {id + 1}", async (ct, _) =>
        {
            var reply = await SendAsync($"diag-check {id}", ct);
            AppendLog(reply);
        });
    }

    private static void EnsureSettingsStopped()
    {
        if (ProcessWatch.ReadLearner().Alive || Enumerable.Range(0, 6).Any(i => ProcessWatch.ReadWorker(i).CollectorAlive))
            throw new InvalidOperationException("Arrête l’apprentissage et les workers avant de modifier les paramètres.");
    }

    private async void BtnRewardSearch_Click(object sender, RoutedEventArgs e)
    {
        if (_busy)
        {
            AppendLog("Opération déjà en cours.");
            return;
        }

        var generation = ++_opGeneration;
        _opCts = new CancellationTokenSource();
        var ct = _opCts.Token;
        var learnerWasOff = !ProcessWatch.ReadLearner().Alive;
        var askedLearner = 0;
        Task? learnerStart = null;
        SetBusy(true, "Essai des paramètres sur les 6 workers…");
        try
        {
            var report = await RewardSearch.RunLiveAsync(ct, msg => Dispatcher.Invoke(() =>
            {
                AppendLog(msg);
                SetStatus(msg);
                if (!msg.Contains("BESOIN_APPRENTISSAGE") || !learnerWasOff || askedLearner != 0)
                    return;
                askedLearner = 1;
                AppendLog("Les voitures sont à l'arrêt. Démarrage de l'apprentissage le temps des essais.");
                learnerStart = StartLearnerForSearchAsync(ct);
            }));
            if (generation != _opGeneration) return;
            SearchSummary.Text = report.Summary;
            RewardSearchGrid.ItemsSource = report.Rows;
            if (report.Rows.Count > 0)
                RewardSearchGrid.SelectedIndex = 0;
            AppendLog(report.Summary);
            SetStatus("Recherche : terminée.");
        }
        catch (OperationCanceledException)
        {
            if (generation != _opGeneration) return;
            SetStatus("Recherche : annulée.");
            AppendLog("Recherche : annulée.");
        }
        catch (Exception ex)
        {
            if (generation != _opGeneration) return;
            SetStatus("Recherche : échouée.");
            AppendLog("Recherche : " + ex.Message);
            MessageBox.Show(this, ex.Message, "Recherche", MessageBoxButton.OK, MessageBoxImage.Error);
        }
        finally
        {
            if (learnerStart is not null)
            {
                try { await learnerStart; }
                catch (Exception startEx) { AppendLog("Démarrage de l'apprentissage : " + startEx.Message); }
            }
            if (askedLearner == 1)
            {
                try
                {
                    var reply = await SendAsync("learner stop", CancellationToken.None);
                    AppendLog(reply);
                    AppendLog("Apprentissage arrêté après la recherche.");
                }
                catch (Exception stopEx)
                {
                    AppendLog("Arrêt de l'apprentissage : " + stopEx.Message);
                }
            }
            if (generation == _opGeneration)
            {
                SetBusy(false);
                _opCts = null;
            }
        }
    }

    private async Task StartLearnerForSearchAsync(CancellationToken ct)
    {
        try
        {
            var reply = await SendAsync("learner start", ct);
            AppendLog(reply);
        }
        catch (Exception ex)
        {
            AppendLog("Démarrage de l'apprentissage : " + ex.Message);
        }
    }

    private void RewardSearchGrid_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        SearchDetail.Text = RewardSearchGrid.SelectedItem is RewardSearchRow row ? row.Detail : "";
    }

    private void BtnRewardApply_Click(object sender, RoutedEventArgs e)
    {
        if (RewardSearchGrid.SelectedItem is not RewardSearchRow row)
        {
            MessageBox.Show(this, "Lance la recherche, puis choisis une ligne.", "Recherche", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }

        try
        {
            EnsureSettingsStopped();
            RewardConfig.Save(row.Fields);
            ReloadRewards();
            AppendLog("Réglages de la recherche enregistrés dans config_first_sac_cpu_v3.yml");
            MessageBox.Show(this,
                "Enregistré. Crée un nouvel entraînement, redémarre les workers, puis démarre l’apprentissage. Sinon la voiture garde ses anciennes habitudes.",
                "Recherche", MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "Recherche", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void BtnRewardReload_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            ReloadRewards();
            AppendLog("Récompenses rechargées.");
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "Récompenses", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void BtnRewardSave_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            EnsureSettingsStopped();
            RewardConfig.Save(_rewards);
            AppendLog("Récompenses enregistrées dans config_first_sac_cpu_v3.yml");
            MessageBox.Show(this,
                "Enregistré. Redémarre les workers puis l’apprentissage pour utiliser les nouvelles valeurs.",
                "Récompenses", MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "Récompenses", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void ReloadRewards()
    {
        _rewards.Clear();
        foreach (var f in RewardConfig.Load())
            _rewards.Add(f);
    }

    private void ReloadSettings()
    {
        _settings.Clear();
        foreach (var f in SettingsConfig.LoadYamlFields())
            _settings.Add(f);

        var prefs = SettingsConfig.LoadUiPrefs();
        _settingsPresetSilent = true;
        try
        {
            SettingsWorkerWidth.Text = prefs.WorkerWidth.ToString();
            SettingsWorkerHeight.Text = prefs.WorkerHeight.ToString();
            SettingsWorkerCount.SelectedIndex = Math.Clamp(prefs.WorkerCount, 1, 6) - 1;
            SettingsGameSound.IsChecked = prefs.GameSound;
            SettingsGameVolume.Text = prefs.GameMasterVolume.ToString(System.Globalization.CultureInfo.InvariantCulture);
            SelectQualityPreset(prefs.QualityPreset);
        }
        finally
        {
            _settingsPresetSilent = false;
        }
        UpdateStartWorkersLabel();
    }

    private void SelectQualityPreset(string preset)
    {
        foreach (var item in SettingsQualityPreset.Items.OfType<System.Windows.Controls.ComboBoxItem>())
        {
            if (string.Equals(item.Tag?.ToString(), preset, StringComparison.OrdinalIgnoreCase))
            {
                SettingsQualityPreset.SelectedItem = item;
                return;
            }
        }
        foreach (var item in SettingsQualityPreset.Items.OfType<System.Windows.Controls.ComboBoxItem>())
        {
            if (string.Equals(item.Tag?.ToString(), "perso", StringComparison.OrdinalIgnoreCase))
            {
                SettingsQualityPreset.SelectedItem = item;
                return;
            }
        }
    }

    private void BtnSettingsReload_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            ReloadSettings();
            AppendLog("Paramètres rechargés.");
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "Paramètres", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void BtnSettingsSave_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            EnsureSettingsStopped();
            if (!int.TryParse(SettingsWorkerWidth.Text.Trim(), out var w)
                || !int.TryParse(SettingsWorkerHeight.Text.Trim(), out var h)
                || !double.TryParse(SettingsGameVolume.Text.Trim().Replace(',', '.'),
                    System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var vol))
                throw new ArgumentException("Largeur et hauteur : nombres entiers. Volume : nombre entre 0 et 1.");
            var prefs = new SettingsConfig.UiPrefs
            {
                WorkerWidth = w,
                WorkerHeight = h,
                WorkerCount = SettingsWorkerCount.SelectedIndex + 1,
                GameSound = SettingsGameSound.IsChecked == true,
                GameMasterVolume = vol,
                QualityPreset = (SettingsQualityPreset.SelectedItem as System.Windows.Controls.ComboBoxItem)?.Tag?.ToString() ?? "leger",
            };
            SettingsConfig.SaveAll(_settings, prefs);
            UpdateStartWorkersLabel();
            var track = prefs.TrackId;
            var car = prefs.CarId;
            AppendLog($"Paramètres enregistrés (circuit={track}, voiture={car}, {prefs.WorkerWidth}x{prefs.WorkerHeight}).");
            MessageBox.Show(this,
                "Enregistré et appliqué.\n\n"
                + "• YAML entraînement\n"
                + "• race.ini de tous les workers (circuit / voiture)\n"
                + "• video.ini (résolution) + audio.ini (son)\n\n"
                + "Redémarre les workers puis l’apprentissage pour appliquer ces paramètres.",
                "Paramètres", MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, ex.Message, "Paramètres", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void SettingsWorkerCount_Changed(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (_settingsPresetSilent || SettingsWorkerCount is null || SettingsWorkerCount.SelectedIndex < 0)
            return;
        var prefs = SettingsConfig.LoadUiPrefs();
        prefs.WorkerCount = SettingsWorkerCount.SelectedIndex + 1;
        SettingsConfig.SaveUiPrefsOnly(prefs);
        UpdateStartWorkersLabel();
    }

    private void SettingsQualityPreset_Changed(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (_settingsPresetSilent || SettingsWorkerWidth is null || SettingsWorkerHeight is null)
            return;
        var tag = (SettingsQualityPreset.SelectedItem as System.Windows.Controls.ComboBoxItem)?.Tag?.ToString();
        if (string.IsNullOrWhiteSpace(tag) || tag.Equals("perso", StringComparison.OrdinalIgnoreCase))
            return;
        var prefs = new SettingsConfig.UiPrefs();
        SettingsConfig.ApplyQualityPreset(prefs, tag);
        SettingsWorkerWidth.Text = prefs.WorkerWidth.ToString();
        SettingsWorkerHeight.Text = prefs.WorkerHeight.ToString();
    }

    private sealed record SaveRow(string Name, string Date, long Steps, string Car, string Track);
    private void RefreshSaves_Click(object sender, RoutedEventArgs e) => ReloadSaves();
    private void ReloadSaves()
    {
        var rows = new List<SaveRow>();
        var folder = Path.Combine(RepoPaths.Root, "outputs", "training_sessions");
        if (Directory.Exists(folder)) foreach (var dir in Directory.GetDirectories(folder))
        {
            var file = Path.Combine(dir, "session.json");
            if (!File.Exists(file)) continue;
            try {
                using var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(file));
                var r = doc.RootElement;
                string Text(string key) => r.TryGetProperty(key, out var value) && value.ValueKind == System.Text.Json.JsonValueKind.String ? value.GetString() ?? "inconnu" : "inconnu";
                rows.Add(new SaveRow(Path.GetFileName(dir), Text("saved_at"), r.TryGetProperty("steps", out var n) ? n.GetInt64() : 0, Text("car"), Text("track")));
            } catch (Exception ex) { AppendLog("Lecture sauvegarde : " + ex.Message); }
        }
        SavesGrid.ItemsSource = rows;
    }
    private async void SaveNamed_Click(object sender, RoutedEventArgs e)
        => await SaveSnapshotAsync(SaveName.Text.Trim());
    private async void NewSave_Click(object sender, RoutedEventArgs e)
    {
        var name = SaveName.Text.Trim();
        if (!System.Text.RegularExpressions.Regex.IsMatch(name, @"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")) { MessageBox.Show("Saisis un nom valide pour le nouvel entraînement."); return; }
        if (MessageBox.Show(this,
                "Créer un modèle neuf sous ce nom ?\n\n" +
                "Après création : NE PAS cliquer sur « Charger ».\n" +
                "Redémarre les workers, puis « Démarrer l’apprentissage ».",
                "Nouvel entraînement", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
        await RunOpAsync("Nouvel entraînement", async (ct, progress) => { await SendAsync("train new " + name, ct); ReloadSaves(); });
    }
    private async void LoadSave_Click(object sender, RoutedEventArgs e)
    {
        if (SavesGrid.SelectedItem is not SaveRow row) return;
        if (MessageBox.Show(this, "Charger " + row.Name + " à la place du modèle courant ? Sauvegarde d’abord le travail courant.", "Charger", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
        await RunOpAsync("Charger", async (ct, progress) => { await SendAsync("train load " + row.Name, ct); ReloadSaves(); });
    }
    private async void DeleteSave_Click(object sender, RoutedEventArgs e)
    {
        if (SavesGrid.SelectedItem is not SaveRow row) return;
        if (MessageBox.Show(this, "Supprimer définitivement " + row.Name + " ?", "Supprimer", MessageBoxButton.YesNo) != MessageBoxResult.Yes) return;
        await RunOpAsync("Supprimer", async (ct, progress) => { await SendAsync("train delete " + row.Name, ct); ReloadSaves(); });
    }

    private async Task RunOpAsync(string name, Func<CancellationToken, Action<string>, Task> work)
    {
        if (_busy)
        {
            AppendLog("Opération déjà en cours.");
            return;
        }

        _opCts = new CancellationTokenSource();
        var ct = _opCts.Token;
        var generation = ++_opGeneration;
        SetBusy(true, name + "…");
        try
        {
            await EnsureClientAsync(ct);
            await work(ct, msg => Dispatcher.Invoke(() => SetStatus(msg)));
            if (generation != _opGeneration) return;
            SetStatus(name + " : terminé.");
            AppendLog(name + " : terminé.");
            RefreshUi();
        }
        catch (OperationCanceledException)
        {
            if (generation != _opGeneration) return;
            SetStatus(name + " : annulé.");
            AppendLog(name + " : annulé.");
        }
        catch (Exception ex)
        {
            if (generation != _opGeneration) return;
            SetStatus(name + " : échoué.");
            AppendLog(name + " : " + ex.Message);
            MessageBox.Show(this, ex.Message + "\n\nTu peux utiliser Arrêt d’urgence si des processus restent bloqués.",
                name, MessageBoxButton.OK, MessageBoxImage.Error);
        }
        finally
        {
            if (generation == _opGeneration)
            {
                SetBusy(false);
                _opCts = null;
            }
        }
    }

    private async Task EnsureClientAsync(CancellationToken ct)
    {
        if (_client?.IsConnected == true)
            return;
        AppendLog("Reconnexion au service…");
        _client?.Dispose();
        var (client, _) = await ServerBootstrap.EnsureConnectedAsync(AppendLog, ct);
        _client = client;
        ConnectionLabel.Text = "Connecté au centre de contrôle";
    }

    private async Task<string> SendAsync(string command, CancellationToken ct)
    {
        if (_client is null)
            throw new InvalidOperationException("Service non connecté.");
        AppendLog("> " + command);
        try
        {
            return await _client.SendCommandAsync(command, ct);
        }
        catch (Exception ex) when (ex is System.IO.IOException
                                   or System.Net.Sockets.SocketException
                                   or InvalidOperationException)
        {
            // La connexion au serveur C4D s'est rompue (serveur redémarré / relancé) :
            // on se reconnecte puis on retente une seule fois, au lieu d'afficher
            // « Unable to read data from the transport connection ».
            AppendLog("Connexion perdue (" + ex.Message + ") — reconnexion…");
            try { _client?.Dispose(); } catch { /* ignore */ }
            var (client, _) = await ServerBootstrap.EnsureConnectedAsync(AppendLog, ct);
            _client = client;
            ConnectionLabel.Text = "Connecté au centre de contrôle";
            return await _client.SendCommandAsync(command, ct);
        }
    }

    private void SetBusy(bool busy, string? status = null)
    {
        _busy = busy;
        BtnStartWorkers.IsEnabled = !busy;
        BtnStartLearner.IsEnabled = !busy;
        BtnSaveLearner.IsEnabled = !busy;
        BtnStopLearner.IsEnabled = !busy;
        BtnStopWorkers.IsEnabled = !busy;
        BtnRewardSearch.IsEnabled = !busy;
        BtnRewardApply.IsEnabled = !busy;
        // Emergency stays available unless we are already inside emergency; keep enabled.
        if (status is not null)
            SetStatus(status);
    }

    private void SetStatus(string text) => StatusLabel.Text = text;

    private void AppendLog(string text)
    {
        if (!Dispatcher.CheckAccess())
        {
            Dispatcher.Invoke(() => AppendLog(text));
            return;
        }
        LogBox.AppendText($"[{DateTime.Now:HH:mm:ss}] {text}\n");
        LogBox.ScrollToEnd();
    }

    private void RenderDiagnostic()
    {
        try
        {
            var workers = Diagnostics.ReadWorkers();
            var (total, perWorker) = Diagnostics.SampleCpu(workers);

            DiagTotalCpu.Text = $"CPU : {total:0} %";
            DiagTotalCpuBar.Value = Math.Clamp(total, 0, 100);
            DiagMeanReward.Text = $"  •  Récompense moyenne : {Diagnostics.ReadMeanReward():0.000}";
            var neuro = Diagnostics.ReadNeuroEvolution();
            DiagNeuro.Text = neuro.PopulationSize > 0
                ? $"Génération {neuro.Generation}  •  candidats {neuro.Evaluated}/{neuro.PopulationSize}  •  meilleur #{neuro.BestIndex} (score {neuro.BestScore:0.0})"
                : "Neuroévolution : pas démarrée";
            UpdateEvolutionTab(neuro);
            DiagRecommendation.Text = Diagnostics.Recommendation(total, workers);

            // Ajout d'un échantillon toutes les ~500 ms (étapes de collecte + CPU).
            const double sampleIntervalSec = 0.5;
            var now = DateTime.UtcNow;
            if (_diagLastSampleAt == DateTime.MinValue || (now - _diagLastSampleAt).TotalSeconds >= sampleIntervalSec)
            {
                _diagLastSampleAt = now;
                foreach (var w in workers)
                {
                    if (!_diagSamples.TryGetValue(w.Id, out var list))
                    {
                        list = new List<DiagSample>();
                        _diagSamples[w.Id] = list;
                    }
                    var steps = 0.0;
                    if (_diagLastTransitions.TryGetValue(w.Id, out var prev))
                        steps = Math.Max(0, w.Transitions - prev);
                    _diagLastTransitions[w.Id] = w.Transitions;
                    // Télémétrie : 1 paquet toutes les 2 images du jeu.
                    var frames = 0.0;
                    if (_diagLastTelemetryFrames.TryGetValue(w.Id, out var prevFrames))
                        frames = Math.Max(0, w.TelemetryFrameCount - prevFrames);
                    _diagLastTelemetryFrames[w.Id] = w.TelemetryFrameCount;
                    var cpu = perWorker.TryGetValue(w.Id, out var c) ? c : 0.0;
                    // Lissage de la vitesse (EMA) : la télémétrie renvoie la vitesse
                    // instantanée, qui saute à chaque téléportation / reprise — les
                    // pics aléatoires viennent de là.
                    var rawSpeed = w.SpeedKmh;
                    var smoothSpeed = _speedEma.TryGetValue(w.Id, out var prevSpeed)
                        ? prevSpeed * 0.6 + rawSpeed * 0.4
                        : rawSpeed;
                    _speedEma[w.Id] = smoothSpeed;
                    list.Add(new DiagSample
                    {
                        StepsPer2s = steps,
                        CpuPercent = cpu,
                        SpeedKmh = smoothSpeed,
                        TelemetryAgeMs = w.TelemetryAgeMs,
                        EpisodeCount = w.EpisodeCount,
                        Reward = w.Reward,
                        GameFps = frames * 2.0 / sampleIntervalSec,
                        TrainHz = steps / sampleIntervalSec,
                        NeuroCandidate = w.NeuroCandidate,
                        NeuroLaps = w.NeuroLaps,
                        NeuroProgressM = w.NeuroProgressM,
                    });
                    while (list.Count > 300) list.RemoveAt(0);
                }
            }

            RenderCarChart(DiagChart0, DiagTitle0, 0, workers);
            RenderCarChart(DiagChart1, DiagTitle1, 1, workers);
            RenderCarChart(DiagChart2, DiagTitle2, 2, workers);
            RenderCarChart(DiagChart3, DiagTitle3, 3, workers);
            RenderCarChart(DiagChart4, DiagTitle4, 4, workers);
            RenderCarChart(DiagChart5, DiagTitle5, 5, workers);
        }
        catch
        {
            // le diagnostic ne doit jamais faire planter le rafraîchissement
        }
    }

    private void UpdateEvolutionTab(NeuroSummary neuro)
    {
        if (neuro.PopulationSize <= 0)
        {
            EvoGeneration.Text = "Génération —";
            EvoStatus.Text = "La neuroévolution n'est pas démarrée. Lance l'entraînement pour suivre la progression ici.";
            EvoProgressBar.Value = 0;
            EvoProgressText.Text = "Candidats évalués : — / —";
            EvoBest.Text = "Meilleur candidat : —";
            return;
        }
        EvoGeneration.Text = $"Génération {neuro.Generation}";
        EvoProgressBar.Maximum = neuro.PopulationSize;
        EvoProgressBar.Value = Math.Min(neuro.Evaluated, neuro.PopulationSize);
        EvoProgressText.Text = $"Candidats évalués : {neuro.Evaluated} / {neuro.PopulationSize}";
        EvoBest.Text = neuro.BestIndex >= 0
            ? $"Meilleur candidat : #{neuro.BestIndex} — score {neuro.BestScore:0.0}"
            : "Meilleur candidat : —";
        EvoStatus.Text = neuro.Evaluated >= neuro.PopulationSize
            ? "Génération terminée — passage à la suivante…"
            : "Évaluation en cours…";
    }

    private void RenderCarChart(Canvas canvas, TextBlock title, int id, IReadOnlyList<DiagWorker> workers)
    {
        canvas.Children.Clear();
        var worker = workers.FirstOrDefault(x => x.Id == id) ?? new DiagWorker { Id = id };

        var phase = worker.CollectorAlive
            ? (string.IsNullOrWhiteSpace(worker.Phase) ? "actif" : worker.Phase)
            : "absente";
        title.Text = $"Voiture {id + 1} — {phase}";
        title.Foreground = HealthBrush(worker);

        var width = canvas.ActualWidth > 0 ? canvas.ActualWidth : 280;
        var height = canvas.Height > 0 ? canvas.Height : 130;
        const double margin = 4;

        // léger quadrillage de fond
        for (var g = 1; g <= 3; g++)
        {
            var gy = margin + (height - 2 * margin) * g / 4.0;
            canvas.Children.Add(new System.Windows.Shapes.Line
            {
                X1 = margin, Y1 = gy, X2 = width - margin, Y2 = gy,
                Stroke = new SolidColorBrush(Color.FromRgb(0xEE, 0xEE, 0xEE)), StrokeThickness = 1,
            });
        }

        if (!_diagSamples.TryGetValue(id, out var samples) || samples.Count < 2)
        {
            var empty = new TextBlock
            {
                Text = worker.CollectorAlive ? "En attente de données…" : "—",
                Foreground = Brushes.Gray, FontSize = 11,
            };
            Canvas.SetLeft(empty, 8);
            Canvas.SetTop(empty, height / 2 - 8);
            canvas.Children.Add(empty);
            return;
        }

        var n = samples.Count;
        var maxSteps = Math.Max(1.0, samples.Max(s => s.StepsPer2s));
        var maxSpeed = Math.Max(1.0, samples.Max(s => s.SpeedKmh));

        var stepsLine = new System.Windows.Shapes.Polyline
        {
            Stroke = new SolidColorBrush(Color.FromRgb(0x27, 0xAE, 0x60)), StrokeThickness = 1.5,
        };
        var cpuLine = new System.Windows.Shapes.Polyline
        {
            Stroke = new SolidColorBrush(Color.FromRgb(0x29, 0x80, 0xB9)), StrokeThickness = 1.5,
        };
        var speedLine = new System.Windows.Shapes.Polyline
        {
            Stroke = new SolidColorBrush(Color.FromRgb(0xE6, 0x7E, 0x22)), StrokeThickness = 1.2,
        };
        var rewardLine = new System.Windows.Shapes.Polyline
        {
            Stroke = new SolidColorBrush(Color.FromRgb(0x8E, 0x44, 0xAD)), StrokeThickness = 1.2,
        };

        for (var i = 0; i < n; i++)
        {
            var x = margin + (width - 2 * margin) * i / (double)(n - 1);
            var ySteps = height - margin - (height - 2 * margin) * (samples[i].StepsPer2s / maxSteps);
            var yCpu = height - margin - (height - 2 * margin) * Math.Min(1.0, samples[i].CpuPercent / 100.0);
            var ySpeed = height - margin - (height - 2 * margin) * (samples[i].SpeedKmh / maxSpeed);
            var yReward = height - margin - (height - 2 * margin) * Math.Clamp((samples[i].Reward + 0.5) / 1.0, 0.0, 1.0);
            stepsLine.Points.Add(new Point(x, ySteps));
            cpuLine.Points.Add(new Point(x, yCpu));
            speedLine.Points.Add(new Point(x, ySpeed));
            rewardLine.Points.Add(new Point(x, yReward));
        }
        canvas.Children.Add(stepsLine);
        canvas.Children.Add(cpuLine);
        canvas.Children.Add(speedLine);
        canvas.Children.Add(rewardLine);

        var last = samples[n - 1];
        var legend = new TextBlock
        {
            Text = $"jeu : {last.GameFps:0} Hz • CPU : {last.CpuPercent:0}% • {last.SpeedKmh:0} km/h • candidat #{last.NeuroCandidate} • tours {last.NeuroLaps} • {last.NeuroProgressM:0} m",
            FontSize = 10, Foreground = Brushes.DimGray,
        };
        Canvas.SetLeft(legend, margin + 2);
        Canvas.SetTop(legend, 0);
        canvas.Children.Add(legend);
    }

    private static Brush HealthBrush(DiagWorker w)
    {
        if (!w.CollectorAlive)
            return new SolidColorBrush(Color.FromRgb(0xBB, 0xBB, 0xBB)); // gris : absente
        if (w.Phase.Contains("error", StringComparison.OrdinalIgnoreCase)
            || w.Phase.Contains("stuck", StringComparison.OrdinalIgnoreCase))
            return new SolidColorBrush(Color.FromRgb(0xC0, 0x39, 0x2B)); // rouge : erreur
        if (w.TelemetryAgeMs > 1500)
            return new SolidColorBrush(Color.FromRgb(0xE6, 0x7E, 0x22)); // orange : télémétrie vieille
        return new SolidColorBrush(Color.FromRgb(0x27, 0xAE, 0x60)); // vert : en forme
    }

    private void PopulateLogPicker()
    {
        _logOptions.Clear();
        _logOptions.Add(("Session (lanceur)", "multi_session_*.log"));
        for (var i = 0; i < 6; i++)
            _logOptions.Add(($"Voiture {i + 1} — erreurs", $"worker_{i}_*_stderr.log"));
        _logOptions.Add(("Serveur de transitions — erreurs", "transition_*_stderr.log"));
        _logOptions.Add(("Apprentissage — dernier démarrage", "learner_start_last.log"));
        _logOptions.Add(("Apprentissage — erreurs", "learner_*_stderr.log"));

        LogPicker.Items.Clear();
        foreach (var (label, _) in _logOptions)
            LogPicker.Items.Add(new ComboBoxItem { Content = label });
        if (LogPicker.Items.Count > 0)
            LogPicker.SelectedIndex = 0;
    }

    private void LogPicker_Changed(object sender, SelectionChangedEventArgs e) => RefreshLogView();

    private void BtnLogsRefresh_Click(object sender, RoutedEventArgs e) => RefreshLogView();

    private void RefreshLogView()
    {
        if (LogPicker.SelectedIndex < 0 || LogPicker.SelectedIndex >= _logOptions.Count)
            return;
        LoadLog(LatestLog(_logOptions[LogPicker.SelectedIndex].Pattern));
    }

    private void LoadLog(string? path)
    {
        _currentLogPath = path ?? "";
        if (string.IsNullOrEmpty(path) || !File.Exists(path))
        {
            LogView.Text = "(aucun log trouvé pour ce fichier)";
            return;
        }
        try
        {
            const long maxBytes = 512 * 1024;
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            var len = fs.Length;
            var start = Math.Max(0, len - maxBytes);
            fs.Seek(start, SeekOrigin.Begin);
            using var sr = new StreamReader(fs);
            var text = sr.ReadToEnd();
            if (start > 0)
                text = $"... (fichier tronqué, {start} octets masqués) ...\n\n" + text;
            LogView.Text = text;
            LogView.ScrollToEnd();
        }
        catch
        {
            LogView.Text = "(impossible de lire le fichier)";
        }
    }

    private static string? LatestLog(string pattern)
    {
        var dir = Path.Combine(RepoPaths.Root, "ac_workers", "logs");
        if (!Directory.Exists(dir))
            return null;
        return Directory.GetFiles(dir, pattern)
            .OrderByDescending(File.GetLastWriteTimeUtc)
            .FirstOrDefault();
    }
}
