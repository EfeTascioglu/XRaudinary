using System;
using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using Debug = UnityEngine.Debug;

public class CaptionStream : MonoBehaviour
{
    [Header("Server")]
    public bool useTestData = false;
    public string serverIP = "";
    public int serverPort = 0;
    public string serverPath = "/";

    [Header("Debug")]
    public bool logConnection = true;
    public bool logRxChunks = false;              // turn on only briefly (spam)
    public bool logMessageHeads = true;           // logs head of each WS message
    public bool logParseSuccess = true;
    public bool logParseFailure = true;
    public bool logCaptionText = true;            // NEW: logs each caption string
    public int logEveryNParses = 1;               // set to 10 if too spammy
    public int headChars = 160;
    public int captionChars = 180;                // NEW: truncate caption logs

    [Header("Test Feed")]
    public float testHz = 8f;
    [TextArea(2, 8)]
    public string[] testJsonMessages;

    // Latest parsed packet (thread-safe publication pattern: replace reference)
    private volatile ServerPacket _latestPacket;
    private volatile string _latestRawJson;
    private long _latestSeq;

    // Optional: event on main thread (not used by your current HUD, but kept)
    public event Action<ServerPacket> OnPacket;

    ClientWebSocket _ws;
    CancellationTokenSource _cts;
    Task _task;

    // Queue packets to main thread (Unity objects must be touched on main thread)
    readonly ConcurrentQueue<ServerPacket> _mainThreadPackets = new ConcurrentQueue<ServerPacket>();

    void Start()
    {
        if (useTestData)
        {
            StartCoroutine(TestLoop());
            return;
        }

        string url = $"ws://{serverIP}:{serverPort}{serverPath}";
        _cts = new CancellationTokenSource();
        _task = ConnectAndReceiveLoop(url, _cts.Token);
    }

    void OnDisable() => StopWs();
    void OnApplicationQuit() => StopWs();

    void StopWs()
    {
        try { _cts?.Cancel(); } catch { }
        _cts?.Dispose();
        _cts = null;

        try
        {
            _ws?.Dispose();
            _ws = null;
        }
        catch { }
    }

    void Update()
    {
        // Deliver packets on main thread
        while (_mainThreadPackets.TryDequeue(out var pkt))
        {
            OnPacket?.Invoke(pkt);
        }
    }

    /// <summary>For HUD: get latest parsed packet (no allocations).</summary>
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
                "{\"localization\":[0.0,1.0,0.0],\"transcription\":\"Hi!\"}",
                "{\"localization\":[0.5,1.0,0.0],\"transcription\":\"Nice to meet you.\"}",
                "{\"localization\":[1.5,1.0,0.0],\"transcription\":\"My name is Bob.\"}",
                "{\"localization\":[3.0,0.7,0.0],\"transcription\":\"What is your name?\"}"
            };
        }

        int i = 0;
        float period = 1f / Mathf.Max(0.1f, testHz);

        while (true)
        {
            HandleIncomingWsMessage(testJsonMessages[i], source: "TEST");
            i = (i + 1) % testJsonMessages.Length;
            yield return new WaitForSeconds(period);
        }
    }

    async Task ConnectAndReceiveLoop(string url, CancellationToken ct)
    {
        _ws = new ClientWebSocket();
        _ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(10);

        try
        {
            if (logConnection) Debug.Log($"[WS] Connecting: {url}");
            await _ws.ConnectAsync(new Uri(url), ct).ConfigureAwait(false);
            if (logConnection) Debug.Log($"[WS] Connected. State={_ws.State}");

            Debug.Log($"[WS] TestedConnection.");

            byte[] buffer = new byte[64 * 1024];

            while (!ct.IsCancellationRequested && _ws.State == WebSocketState.Open)
            {
                var sb = new StringBuilder();
                WebSocketReceiveResult result;

                do
                {
                    result = await _ws.ReceiveAsync(new ArraySegment<byte>(buffer), ct).ConfigureAwait(false);

                    if (logRxChunks)
                        Debug.Log($"[WS] RX chunk bytes={result.Count} end={result.EndOfMessage} type={result.MessageType} state={_ws.State}");

                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        if (logConnection)
                            Debug.Log($"[WS] Close from server. Status={_ws.CloseStatus} Desc={_ws.CloseStatusDescription}");

                        try
                        {
                            await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "Ack close", CancellationToken.None).ConfigureAwait(false);
                        }
                        catch { }

                        if (logConnection)
                            Debug.Log($"[WS] Closed. FinalState={_ws.State}");

                        return;
                    }

                    if (result.Count > 0)
                        sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                }
                while (!result.EndOfMessage);

                string msg = sb.ToString();
                Debug.Log($"[WS] RX message chars={msg.Length}");
                if (!string.IsNullOrWhiteSpace(msg))
                    HandleIncomingWsMessage(msg, source: "WS");
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

            if (logConnection)
                Debug.Log("[WS] Receive loop ended.");
        }
    }

    void HandleIncomingWsMessage(string msg, string source)
    {
        if (logMessageHeads)
        {
            string head = msg.Replace("\r", "\\r").Replace("\n", "\\n");
            if (head.Length > headChars) head = head.Substring(0, headChars) + "...";
            Debug.Log($"[WS:{source}] msgLen={msg.Length} head={head}");
        }

        int parsedCount = 0;
        string normalized = NormalizePossiblyQuotedJson(msg);

        Debug.Log($"[WS:{source}] normalizedLen={normalized?.Length ?? -1}");

        foreach (var jsonObj in ExtractJsonObjects(normalized))
        {
            if (TryParsePacket(jsonObj, out var pkt, out var err))
            {
                long seq = ++_latestSeq; // only advance on success
                _latestRawJson = jsonObj;
                _latestPacket = pkt;

                _mainThreadPackets.Enqueue(pkt);
                parsedCount++;

                // Parse-level debug (size + localization presence)
                if (logParseSuccess && (seq % Mathf.Max(1, logEveryNParses) == 0))
                {
                    string caption = pkt.GetCaptionText();
                    int locLen = pkt.localization?.Length ?? -1;
                    Debug.Log($"[PARSE:OK] seq={seq} textLen={caption?.Length ?? 0} locLen={locLen}");
                }

                // NEW: caption text debug (what you actually want)
                if (logCaptionText)
                {
                    string caption = pkt.GetCaptionText() ?? "";
                    if (caption.Length > captionChars) caption = caption.Substring(0, captionChars) + "...";
                    Debug.Log($"[CAPTION] seq={seq} \"{caption}\"");
                }
            }
            else
            {
                long seqStable = _latestSeq;

                if (logParseFailure && (seqStable % Mathf.Max(1, logEveryNParses) == 0))
                {
                    string head = jsonObj.Replace("\r", "\\r").Replace("\n", "\\n");
                    if (head.Length > headChars) head = head.Substring(0, headChars) + "...";
                    Debug.LogWarning($"[PARSE:FAIL] seq={seqStable} err={err} head={head}");
                }
            }
        }

        if (logMessageHeads)
            Debug.Log($"[WS:{source}] extracted {parsedCount} parsable packet(s)");
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
            bool hasLoc = (pkt.localization != null && pkt.localization.Length >= 2);
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

            // If the JSON was transported as an escaped string, unescape common sequences.
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

    // NOTE: ServerPacket is assumed to exist in your project already (CaptionMessage.cs),
    // including: string person_id; float[] localization; GetCaptionText().
}