using TMPro;
using UnityEngine;
using UnityEngine.UI;

public class BottomCaptionHUD : MonoBehaviour
{
    [Header("References")]
    public CaptionStream client;
    public Transform hmd;
    public TMP_FontAsset fontAsset;

    [Header("Placement")]
    public float distance = 0.85f;
    public float verticalOffset = -0.18f;
    public float forwardOffset = 0f;

    [Header("Timing")]
    public float visibleSeconds = 3.0f;
    public float fadeSeconds = 0.6f;

    [Header("Style")]
    public Vector2 panelSize = new Vector2(520f, 90f);
    public float worldScale = 0.0018f;
    public float fontSize = 22f;
    public Color panelColor = new Color(0f, 0f, 0f, 0.55f);
    public Color textColor = Color.white;

    private RectTransform _root;
    private Image _panel;
    private TextMeshProUGUI _text;

    private long _lastSeqSeen = -1;
    private float _lastCaptionTime = -999f;

    void Awake()
    {
        BuildUi();
        _text.text = "HUD READY";
        SetAlpha(1f);
    }

    void LateUpdate()
    {
        if (client == null || hmd == null)
            return;

        ProcessLatestPacket();

        float age = Time.time - _lastCaptionTime;
        float totalLifetime = visibleSeconds + fadeSeconds;

        PlaceHud();

        if (age > totalLifetime)
        {
            SetAlpha(0f);
            return;
        }

        SetAlpha(ComputeAlpha(age));
    }

    void ProcessLatestPacket()
    {
        if (!client.TryGetLatestPacket(out var pkt, out var seq, out _))
            return;

        if (seq == _lastSeqSeen)
            return;

        _lastSeqSeen = seq;

        string caption = pkt?.GetCaptionText() ?? "";
        Debug.Log($"[WS:BottomCaptionHUD] seq={seq} caption=<{caption}>");

        if (string.IsNullOrWhiteSpace(caption))
            return;

        _text.text = caption;
        _lastCaptionTime = Time.time;
}

    void PlaceHud()
    {
        Vector3 pos =
            hmd.position +
            hmd.forward * distance +
            hmd.forward * forwardOffset +
            hmd.up * verticalOffset;

        Vector3 forward = hmd.forward;
        forward.y = 0f;
        if (forward.sqrMagnitude < 1e-6f)
            forward = Vector3.forward;
        forward.Normalize();

        transform.position = pos;
        transform.rotation = Quaternion.LookRotation(forward, Vector3.up);
    }

    float ComputeAlpha(float age)
    {
        if (age <= visibleSeconds)
            return 1f;

        if (fadeSeconds <= 1e-5f)
            return 0f;

        float t = (age - visibleSeconds) / fadeSeconds;
        return Mathf.Clamp01(1f - t);
    }

    void SetAlpha(float alpha)
    {
        alpha = Mathf.Clamp01(alpha);

        if (_panel != null)
        {
            Color pc = panelColor;
            pc.a *= alpha;
            _panel.color = pc;
        }

        if (_text != null)
        {
            Color tc = textColor;
            tc.a *= alpha;
            _text.color = tc;
        }
    }

    void BuildUi()
    {
        gameObject.name = "BottomCaptionHUD";

        var canvas = gameObject.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.WorldSpace;

        _root = gameObject.GetComponent<RectTransform>();
        if (_root == null)
            _root = gameObject.AddComponent<RectTransform>();
        _root.sizeDelta = panelSize;

        var scaler = gameObject.AddComponent<CanvasScaler>();
        scaler.dynamicPixelsPerUnit = 1000f;

        var panelGO = new GameObject("Panel", typeof(RectTransform), typeof(Image));
        panelGO.transform.SetParent(transform, false);

        var panelRect = panelGO.GetComponent<RectTransform>();
        panelRect.anchorMin = new Vector2(0.5f, 0.5f);
        panelRect.anchorMax = new Vector2(0.5f, 0.5f);
        panelRect.pivot = new Vector2(0.5f, 0.5f);
        panelRect.sizeDelta = panelSize;

        _panel = panelGO.GetComponent<Image>();
        _panel.color = panelColor;

        var textGO = new GameObject("Text", typeof(RectTransform));
        textGO.transform.SetParent(panelGO.transform, false);

        _text = textGO.AddComponent<TextMeshProUGUI>();
        if (fontAsset != null)
            _text.font = fontAsset;

        _text.text = "HUD TEST";
        _text.alignment = TextAlignmentOptions.Center;
        _text.fontSize = fontSize;
        _text.color = textColor;
        _text.textWrappingMode = TextWrappingModes.Normal;
        _text.overflowMode = TextOverflowModes.Ellipsis;

        var textRect = textGO.GetComponent<RectTransform>();
        textRect.anchorMin = Vector2.zero;
        textRect.anchorMax = Vector2.one;
        textRect.offsetMin = new Vector2(18f, 10f);
        textRect.offsetMax = new Vector2(-18f, -10f);

        transform.localScale = Vector3.one * worldScale;
    }
}