using UnityEngine;
using UnityEngine.UI;
using TMPro;

public class CaptionHUD : MonoBehaviour
{
    public CaptionStream client;
    public Transform hmd;

    public float anchorDistance = 3.0f;
    public float verticalOffset = 0.8f;
    public float offscreenTopOffset = 0.1f;

    public float inWindowMinDeg = 70f;
    public float inWindowMaxDeg = 110f;

    public float yawSmoothingTau = 0.15f;

    public float staleSeconds = 1.5f;
    public float fadeOutSeconds = 0.8f;

    public Vector2 boxSize = new Vector2(220f, 42f);
    public float worldScale = 0.004f;
    public float fontSize = 12f;
    public float arrowFontSize = 64f;

    public string startupCaption = "Welcome! Initializing...";
    public float startupVisibleSeconds = 2.0f;

    public bool logCaptionsToLogcat = true;
    public int logEveryNCaptionUpdates = 1;
    public bool logYawDebug = false;

    private int _captionLogCount = 0;
    private long _lastSeqSeen = -1;

    RectTransform _rootRect;
    Image _panelImg;
    TextMeshProUGUI _tmp;
    TextMeshProUGUI _arrowTmp;

    float _yawFiltered;
    float _yawRaw;
    float _lastMsgTime = -999f;
    bool _hasDirQuest;
    Vector3 _dirQuest = Vector3.forward;

    void Awake()
    {
        CreateWorldHud();

        if (_tmp != null) _tmp.text = startupCaption;
        _lastMsgTime = Time.time;
        _hasDirQuest = true;
        _dirQuest = Vector3.forward;

        ApplyAlpha(1f);
        SetArrow("");

        Debug.Log("[HUD] Awake() - created world HUD + startup caption");
    }

    void LateUpdate()
    {
        if (hmd == null) return;

        if (client != null)
            ProcessLatest();

        float age = Time.time - _lastMsgTime;
        float alpha = (Time.timeSinceLevelLoad < startupVisibleSeconds) ? 1f : ComputeAlpha(age);
        ApplyAlpha(alpha);

        if (alpha <= 0.001f) return;

        if (!_hasDirQuest)
        {
            _dirQuest = Vector3.forward;
            _hasDirQuest = true;
        }

        float yaw = _yawRaw;
        float yawDeg360 = Mathf.Repeat(yaw * Mathf.Rad2Deg + 360f, 360f);

        bool inWindow = (yawDeg360 >= inWindowMinDeg && yawDeg360 <= inWindowMaxDeg);
        bool outOfFov = !inWindow;

        if (logYawDebug)
        {
            Debug.Log($"[HUD:YAW] yawRad={yaw:F3} yawDeg360={yawDeg360:F1} inWindow={inWindow}");
        }

        if (!outOfFov)
        {
            Vector3 dirWorld = hmd.rotation * _dirQuest;
            transform.position = hmd.position + dirWorld * anchorDistance + hmd.up * verticalOffset;
            SetArrow("");
        }
        else
        {
            transform.position =
                hmd.position +
                hmd.forward * anchorDistance +
                hmd.up * (verticalOffset + offscreenTopOffset);

            SetArrow((_dirQuest.x < 0f) ? "←" : "→");
        }

        transform.rotation = Quaternion.LookRotation(transform.position - hmd.position, hmd.up);
    }

    void ProcessLatest()
    {
        if (client == null) return;

        if (!client.TryGetLatestPacket(out var pkt, out var seq, out var rawJson))
            return;

        if (seq == _lastSeqSeen) return;
        _lastSeqSeen = seq;

        bool gotAny = false;

        string caption = pkt.GetCaptionText();
        if (!string.IsNullOrWhiteSpace(caption))
        {
            if (_tmp != null) _tmp.text = caption;
            _lastMsgTime = Time.time;
            gotAny = true;

            if (logCaptionsToLogcat)
            {
                _captionLogCount++;
                if (_captionLogCount % Mathf.Max(1, logEveryNCaptionUpdates) == 0)
                    Debug.Log($"[HUD:CAPTION] seq={seq} \"{caption}\"");
            }
        }

        if (pkt.localization != null && pkt.localization.Length >= 3)
        {
            float x = pkt.localization[0];
            float y = pkt.localization[1];

            Vector3 quest = new Vector3(x, 0f, y);

            if (quest.sqrMagnitude > 1e-8f)
            {
                _dirQuest = quest.normalized;
                _hasDirQuest = true;

                float yaw = Mathf.Atan2(_dirQuest.z, _dirQuest.x);
                _yawRaw = yaw;

                float dt = Mathf.Max(0f, Time.deltaTime);
                _yawFiltered = SmoothExp(_yawFiltered, yaw, dt, yawSmoothingTau);

                _lastMsgTime = Time.time;
                gotAny = true;
            }
        }

        if (!gotAny) return;
    }

    static float SmoothExp(float current, float target, float dt, float tau)
    {
        if (tau <= 1e-5f) return target;
        float a = 1f - Mathf.Exp(-dt / tau);
        return current + a * (target - current);
    }

    float ComputeAlpha(float age)
    {
        if (age <= staleSeconds) return 1f;
        float t = (age - staleSeconds) / Mathf.Max(1e-5f, fadeOutSeconds);
        return Mathf.Clamp01(1f - t);
    }

    void ApplyAlpha(float a)
    {
        if (_tmp != null) { var c = _tmp.color; c.a = a; _tmp.color = c; }
        if (_arrowTmp != null) { var c = _arrowTmp.color; c.a = a; _arrowTmp.color = c; }
        if (_panelImg != null) { var c = _panelImg.color; c.a = 0.55f * a; _panelImg.color = c; }
    }

    void SetArrow(string s)
    {
        if (_arrowTmp != null) _arrowTmp.text = s;
    }

    void CreateWorldHud()
    {
        gameObject.name = "CaptionHUD_World";

        var canvas = gameObject.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.WorldSpace;

        _rootRect = gameObject.GetComponent<RectTransform>();
        if (_rootRect == null) _rootRect = gameObject.AddComponent<RectTransform>();
        _rootRect.sizeDelta = boxSize;

        var scaler = gameObject.AddComponent<CanvasScaler>();
        scaler.dynamicPixelsPerUnit = 1000f;

        var panelGO = new GameObject("Panel", typeof(RectTransform), typeof(Image));
        panelGO.transform.SetParent(transform, false);

        var panelRect = panelGO.GetComponent<RectTransform>();
        panelRect.sizeDelta = boxSize;

        _panelImg = panelGO.GetComponent<Image>();
        _panelImg.color = new Color(0f, 0f, 0f, 0.55f);

        var textGO = new GameObject("Text", typeof(RectTransform));
        textGO.transform.SetParent(panelGO.transform, false);

        _tmp = textGO.AddComponent<TextMeshProUGUI>();
        _tmp.text = "HUD TEST";
        _tmp.alignment = TextAlignmentOptions.Center;
        _tmp.fontSize = fontSize;
        _tmp.color = Color.white;
        _tmp.textWrappingMode = TextWrappingModes.NoWrap;

        var textRect = textGO.GetComponent<RectTransform>();
        textRect.anchorMin = Vector2.zero;
        textRect.anchorMax = Vector2.one;
        textRect.offsetMin = new Vector2(40f, 0f);
        textRect.offsetMax = new Vector2(-40f, 0f);

        var arrowGO = new GameObject("Arrow", typeof(RectTransform));
        arrowGO.transform.SetParent(panelGO.transform, false);

        _arrowTmp = arrowGO.AddComponent<TextMeshProUGUI>();
        _arrowTmp.text = "";
        _arrowTmp.alignment = TextAlignmentOptions.Center;
        _arrowTmp.fontSize = arrowFontSize;
        _arrowTmp.color = Color.white;
        _arrowTmp.textWrappingMode = TextWrappingModes.NoWrap;

        var arrowRect = arrowGO.GetComponent<RectTransform>();
        arrowRect.anchorMin = new Vector2(0.5f, 1.0f);
        arrowRect.anchorMax = new Vector2(0.5f, 1.0f);
        arrowRect.sizeDelta = new Vector2(120f, 60f);
        arrowRect.pivot = new Vector2(0.5f, 0.0f);
        arrowRect.anchoredPosition = new Vector2(0f, 4f);

        transform.localScale = Vector3.one * worldScale;

        ApplyAlpha(1f);

        _panelImg.enabled = false;
    }
}