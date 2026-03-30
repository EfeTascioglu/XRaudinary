using System;
using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using Debug = UnityEngine.Debug;

[Serializable]
public class ServerPacket
{
    public float[] localization;
    public string transcription;

    public string transcript;
    public string text;

    public string GetCaptionText() => transcription ?? transcript ?? text ?? "";
}

public class CaptionStream : MonoBehaviour
{
    [Header("Server")]
    public bool autoStart = true;
    public bool useTestData = false;
    public string serverIP = "";
    public int serverPort = 0;
    public string serverPath = "/";

    [Header("Debug")]
    public bool logConnection = true;
    public bool logParseFailure = true;
    public bool logCaptionText = false;
    public int captionChars = 180;

    [Header("Test Feed")]
    public float testHz = 1.5f;
    [TextArea(2, 8)]
    public string[] testJsonMessages;

    private volatile ServerPacket _latestPacket;
    private volatile string _latestRawJson;
    private long _latestSeq;

    public event Action<ServerPacket> OnPacket;

    private ClientWebSocket _ws;
    private CancellationTokenSource _cts;
    private Task _task;

    private readonly ConcurrentQueue<ServerPacket> _mainThreadPackets = new();
    private bool _isStarting = false;
    private Coroutine _testLoopCoroutine;

    public bool IsConnected => _ws != null && _ws.State == WebSocketState.Open;
    public string CurrentUrl => $"ws://{serverIP}:{serverPort}{serverPath}";

    void Start()
    {
        if (autoStart)
            StartStream();
    }

    void OnDisable() => StopStream();
    void OnApplicationQuit() => StopStream();

    public void StartStream()
    {
        if (_isStarting) return;

        if (useTestData)
        {
            if (_testLoopCoroutine == null)
                _testLoopCoroutine = StartCoroutine(TestLoop());
            return;
        }

        _cts = new CancellationTokenSource();
        _task = ConnectAndReceiveLoop(CurrentUrl, _cts.Token);
    }

    public void StopStream()
    {
        if (_testLoopCoroutine != null)
        {
            StopCoroutine(_testLoopCoroutine);
            _testLoopCoroutine = null;
        }

        try { _cts?.Cancel(); } catch { }

        try
        {
            _ws?.Dispose();
            _ws = null;
        }
        catch { }

        try
        {
            _cts?.Dispose();
            _cts = null;
        }
        catch { }

        _isStarting = false;
    }

    public void ForceReconnect()
    {
        if (useTestData)
        {
            Debug.Log("[WS] ForceReconnect ignored because useTestData=true.");
            return;
        }

        Debug.Log($"[WS] Reconnecting to {CurrentUrl}");
        StopStream();
        StartStream();
    }

    void Update()
    {
        while (_mainThreadPackets.TryDequeue(out var pkt))
            OnPacket?.Invoke(pkt);
    }

    public bool TryGetLatestPacket(out ServerPacket pkt, out long seq, out string rawJson)
    {
        pkt = _latestPacket;
        seq = _latestSeq;
        rawJson = _latestRawJson;
        return pkt != null;
    }

    System.Collections.IEnumerator TestLoop()
    {
        if (testJsonMessages == null || testJsonMessages.Length == 0)
        {
            testJsonMessages = new[]
            {
                "{\"transcription\":\"Hello from the test caption stream.\"}",
                "{\"transcription\":\"This should appear at the bottom of the screen.\"}",
                "{\"transcription\":\"Face tracking stays untouched in this version.\"}"
            };
        }

        int i = 0;
        float period = 1f / Mathf.Max(0.1f, testHz);

        while (true)
        {
            HandleIncomingWsMessage(testJsonMessages[i], "TEST");
            i = (i + 1) % testJsonMessages.Length;
            yield return new WaitForSeconds(period);
        }
    }

    async Task ConnectAndReceiveLoop(string url, CancellationToken ct)
    {
        _isStarting = true;
        _ws = new ClientWebSocket();
        _ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(10);

        try
        {
            if (logConnection) Debug.Log($"[WS] Connecting: {url}");
            await _ws.ConnectAsync(new Uri(url), ct).ConfigureAwait(false);
            if (logConnection) Debug.Log($"[WS] Connected. State={_ws.State}");

            byte[] buffer = new byte[64 * 1024];

            while (!ct.IsCancellationRequested && _ws.State == WebSocketState.Open)
            {
                var sb = new StringBuilder();
                WebSocketReceiveResult result;

                do
                {
                    result = await _ws.ReceiveAsync(new ArraySegment<byte>(buffer), ct).ConfigureAwait(false);

                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        try
                        {
                            await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "Ack close", CancellationToken.None).ConfigureAwait(false);
                        }
                        catch { }

                        return;
                    }

                    if (result.Count > 0)
                        sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                }
                while (!result.EndOfMessage);

                string msg = sb.ToString();
                if (!string.IsNullOrWhiteSpace(msg))
                    HandleIncomingWsMessage(msg, "WS");
            }
        }
        catch (OperationCanceledException)
        {
            if (logConnection) Debug.Log("[WS] Canceled.");
        }
        catch (Exception e)
        {
            Debug.LogWarning($"[WS] Error:\n{e}");
        }
        finally
        {
            try
            {
                if (_ws != null)
                {
                    if (_ws.State == WebSocketState.Open)
                        await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "Client closing", CancellationToken.None).ConfigureAwait(false);

                    _ws.Dispose();
                    _ws = null;
                }
            }
            catch { }

            _isStarting = false;

            if (logConnection)
                Debug.Log("[WS] Receive loop ended.");
        }
    }

    void HandleIncomingWsMessage(string msg, string source)
    {
        string normalized = NormalizePossiblyQuotedJson(msg);

        foreach (var jsonObj in ExtractJsonObjects(normalized))
        {
            if (TryParsePacket(jsonObj, out var pkt, out var err))
            {
                long seq = ++_latestSeq;
                _latestRawJson = jsonObj;
                _latestPacket = pkt;
                _mainThreadPackets.Enqueue(pkt);

                if (logCaptionText)
                {
                    string caption = pkt.GetCaptionText() ?? "";
                    if (caption.Length > captionChars) caption = caption.Substring(0, captionChars) + "...";
                    Debug.Log($"[CAPTION:{source}] seq={seq} \"{caption}\"");
                }
            }
            else
            {
                if (logParseFailure)
                    Debug.LogWarning($"[PARSE:FAIL] {err}");
            }
        }
    }

    static bool TryParsePacket(string json, out ServerPacket pkt, out string err)
    {
        pkt = null;
        err = null;

        int i0 = json.IndexOf('{');
        if (i0 > 0) json = json.Substring(i0);
        json = json.Trim();

        try
        {
            pkt = JsonUtility.FromJson<ServerPacket>(json);
            if (pkt == null) { err = "FromJson returned null"; return false; }

            bool hasText = !string.IsNullOrWhiteSpace(pkt.GetCaptionText());
            bool hasLoc = pkt.localization != null && pkt.localization.Length >= 2;
            if (!hasText && !hasLoc) { err = "no text and no localization"; return false; }

            return true;
        }
        catch (Exception e)
        {
            err = e.Message;
            return false;
        }
    }

    static System.Collections.Generic.IEnumerable<string> ExtractJsonObjects(string s)
    {
        if (string.IsNullOrEmpty(s)) yield break;

        int depth = 0;
        int start = -1;
        bool inString = false;
        bool escape = false;

        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];

            if (inString)
            {
                if (escape) { escape = false; continue; }
                if (c == '\\') { escape = true; continue; }
                if (c == '"') inString = false;
                continue;
            }

            if (c == '"') { inString = true; continue; }

            if (c == '{')
            {
                if (depth == 0) start = i;
                depth++;
            }
            else if (c == '}')
            {
                depth--;
                if (depth == 0 && start >= 0)
                {
                    yield return s.Substring(start, i - start + 1);
                    start = -1;
                }
            }
        }
    }

    static string NormalizePossiblyQuotedJson(string msg)
    {
        if (string.IsNullOrEmpty(msg)) return msg;

        int first = msg.IndexOf('{');
        int last = msg.LastIndexOf('}');
        if (first >= 0 && last > first)
        {
            string s = msg.Substring(first, last - first + 1);

            if (s.Contains("\\\"") || s.Contains("\\n") || s.Contains("\\r") || s.Contains("\\t"))
            {
                s = s.Replace("\\\"", "\"")
                     .Replace("\\n", "\n")
                     .Replace("\\r", "\r")
                     .Replace("\\t", "\t")
                     .Replace("\\\\", "\\");
            }
            return s.Trim();
        }

        return msg;
    }
}