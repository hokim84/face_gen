// NPC 페이스 스프라이트 표시 컴포넌트
// 사용법:
//   1) pipeline.py 결과 output/<id>/ 폴더들을 Assets/Resources/Portraits/ 아래로 복사
//      (Assets/Resources/Portraits/npc_0001/base.png, expr/smile.png, meta.json ...)
//   2) UI 에 RawImage 두 개(Base, Overlay) 를 만들고 Overlay 를 Base 의 자식으로 둔다
//   3) 이 컴포넌트에 연결 후  portrait.SetCharacter("npc_0001"); portrait.SetExpression("smile");
using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;

[Serializable]
public class PortraitExpression
{
    public string name;
    public string file;   // 빈 문자열이면 오버레이 없음(= base 그대로, neutral)
    public int x, y, w, h; // base 이미지 기준 픽셀 좌표 (좌상단 원점)
}

[Serializable]
public class PortraitMeta
{
    public string id;
    public string name;
    public int width;
    public int height;
    public List<PortraitExpression> expressions;
}

public class NpcPortrait : MonoBehaviour
{
    public RawImage baseImage;
    public RawImage overlayImage;
    public string resourcesRoot = "Portraits";

    PortraitMeta meta;
    public PortraitMeta Meta => meta;

    public bool SetCharacter(string id)
    {
        var ta = Resources.Load<TextAsset>($"{resourcesRoot}/{id}/meta");
        if (ta == null) { Debug.LogError($"[NpcPortrait] meta 없음: {id}"); return false; }
        meta = JsonUtility.FromJson<PortraitMeta>(ta.text);

        baseImage.texture = Resources.Load<Texture2D>($"{resourcesRoot}/{id}/base");
        var rt = baseImage.rectTransform;
        // 가로 크기는 레이아웃에서 정하고, 세로는 원본 비율에 맞춤
        rt.SetSizeWithCurrentAnchors(RectTransform.Axis.Vertical, rt.rect.width * meta.height / (float)meta.width);

        overlayImage.rectTransform.SetParent(rt, false);
        SetExpression("neutral");
        return true;
    }

    public bool HasExpression(string exprName) =>
        meta != null && meta.expressions.Exists(e => e.name == exprName);

    public void SetExpression(string exprName)
    {
        if (meta == null) return;
        var e = meta.expressions.Find(x => x.name == exprName);
        if (e == null)
        {
            Debug.LogWarning($"[NpcPortrait] {meta.id} 에 '{exprName}' 표정 없음 → neutral");
            e = meta.expressions.Find(x => x.name == "neutral");
        }
        if (e == null || string.IsNullOrEmpty(e.file)) { overlayImage.enabled = false; return; }

        string path = $"{resourcesRoot}/{meta.id}/{System.IO.Path.ChangeExtension(e.file, null)}";
        overlayImage.texture = Resources.Load<Texture2D>(path);
        overlayImage.enabled = overlayImage.texture != null;

        float s = baseImage.rectTransform.rect.width / meta.width;
        var ort = overlayImage.rectTransform;
        ort.anchorMin = ort.anchorMax = new Vector2(0, 1);
        ort.pivot = new Vector2(0, 1);
        ort.anchoredPosition = new Vector2(e.x * s, -e.y * s);
        ort.sizeDelta = new Vector2(e.w * s, e.h * s);
    }
}
