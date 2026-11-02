using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace AcAiControl;

internal sealed class C4dClient : IDisposable
{
    private readonly object _gate = new();
    private TcpClient? _client;
    private NetworkStream? _stream;
    private readonly byte[] _recvBuffer = new byte[8192];
    private readonly List<byte> _pending = new();

    public bool IsConnected
    {
        get
        {
            lock (_gate)
                return _client?.Connected == true;
        }
    }

    public async Task ConnectAsync(string host, int port, string token, string repoRoot, CancellationToken ct = default)
    {
        Disconnect();
        var client = new TcpClient();
        await client.ConnectAsync(host, port, ct).ConfigureAwait(false);
        var stream = client.GetStream();

        var hello = new Dictionary<string, object?>
        {
            ["t"] = "hello",
            ["role"] = "console",
            ["client_pid"] = Environment.ProcessId,
            ["token"] = token,
            ["repo_root"] = repoRoot,
        };
        await SendRawAsync(stream, hello, ct).ConfigureAwait(false);
        var ack = await RecvLineAsync(stream, ct).ConfigureAwait(false);
        if (ack is null || !ack.Value.TryGetProperty("t", out var t) || t.GetString() != "hello_ok")
        {
            client.Dispose();
            string? err = null;
            if (ack is { } a && a.TryGetProperty("error", out var e))
                err = e.GetString();
            throw new InvalidOperationException("C4D: " + (err ?? "handshake failed"));
        }

        lock (_gate)
        {
            _client = client;
            _stream = stream;
            _pending.Clear();
        }
    }

    public async Task<string> SendCommandAsync(string line, CancellationToken ct = default)
    {
        NetworkStream stream;
        lock (_gate)
        {
            if (_stream is null || _client?.Connected != true)
                throw new InvalidOperationException("Service non connecté.");
            stream = _stream;
        }

        await SendRawAsync(stream, new Dictionary<string, object?> { ["t"] = "cmd", ["line"] = line }, ct)
            .ConfigureAwait(false);

        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var msg = await RecvLineAsync(stream, ct).ConfigureAwait(false);
            if (msg is null)
                throw new IOException("Centre de contrôle déconnecté.");
            if (msg.Value.TryGetProperty("t", out var kind) && kind.GetString() == "reply")
                return msg.Value.TryGetProperty("text", out var text) ? text.GetString() ?? "" : "";
        }
    }

    public void Disconnect()
    {
        lock (_gate)
        {
            try { _stream?.Dispose(); } catch { /* ignore */ }
            try { _client?.Dispose(); } catch { /* ignore */ }
            _stream = null;
            _client = null;
            _pending.Clear();
        }
    }

    public void Dispose() => Disconnect();

    private static async Task SendRawAsync(NetworkStream stream, object payload, CancellationToken ct)
    {
        var json = JsonSerializer.Serialize(payload) + "\n";
        var bytes = Encoding.UTF8.GetBytes(json);
        await stream.WriteAsync(bytes, ct).ConfigureAwait(false);
        await stream.FlushAsync(ct).ConfigureAwait(false);
    }

    private async Task<JsonElement?> RecvLineAsync(NetworkStream stream, CancellationToken ct)
    {
        while (true)
        {
            var nl = _pending.IndexOf((byte)'\n');
            if (nl >= 0)
            {
                var lineBytes = _pending.GetRange(0, nl).ToArray();
                _pending.RemoveRange(0, nl + 1);
                var text = Encoding.UTF8.GetString(lineBytes).Trim();
                if (string.IsNullOrEmpty(text))
                    continue;
                using var doc = JsonDocument.Parse(text);
                return doc.RootElement.Clone();
            }

            var read = await stream.ReadAsync(_recvBuffer.AsMemory(0, _recvBuffer.Length), ct).ConfigureAwait(false);
            if (read == 0)
                return null;
            for (var i = 0; i < read; i++)
                _pending.Add(_recvBuffer[i]);
        }
    }
}
