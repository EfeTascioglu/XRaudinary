using System;
using UnityEngine;

[Serializable]
public class ServerPacket
{
    public float[] localization;
    public string transcription;

    public string transcript;
    public string text;

    public string GetCaptionText() => transcription ?? transcript ?? text ?? "";
}

public struct CaptionMessage
{
    public Vector3 localizationMic; // vector received from the microphone array
    public Vector3 localizationQuest; // vector after transformed to the Quest frame
    public float yawRad; // projected vector angle in the Quest frame
    public string text; // caption

    public static CaptionMessage FromPacket(ServerPacket pkt, Func<Vector3, Vector3> micToQuest)
    {
        Vector3 mic = Vector3.zero;
        if (pkt?.localization != null && pkt.localization.Length >= 3)
            mic = new Vector3(pkt.localization[0], pkt.localization[2], pkt.localization[1]);

        Vector3 q3 = (micToQuest != null) ? micToQuest(mic) : mic;

        // planar in Unity/Quest convention: x=right, z=forward
        Vector3 qPlanar = new Vector3(q3.x, 0f, q3.z);

        float yaw = (qPlanar.sqrMagnitude < 1e-8f) ? 0f : Mathf.Atan2(qPlanar.x, qPlanar.z);

        return new CaptionMessage
        {
            localizationMic = mic,
            localizationQuest = qPlanar,   // (if you keep only one, keep planar)
            yawRad = yaw,
            text = pkt?.GetCaptionText() ?? ""
        };
    }
}