#!/usr/bin/env python3
"""吹き替えの声選び（実験）：元の音から登場人物ごとの声の傾向（高さ・声の特徴）を測り、近い VOICEVOX の声を選ぶ。
dub-venv（sherpa-onnx・numpy・soundfile）で動かす:

    ~/.local/share/dub-venv/bin/python voicefit.py VIDEO_ID [--src opus] [--out fit]

~/.cache/autotube/dub/VIDEO_ID.{src}.script.json の配役（who）を使い、声を選び直した台本を VIDEO_ID.{out}.script.json に書く。
誰のセリフかが少し外れていても、登場人物ごとに全部のセリフを平均するので、声の傾向はだいたい出る
"""
import argparse
import io
import json
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import numpy as np
import sherpa_onnx
import soundfile as sf

D = Path.home() / ".cache/autotube/dub"
MODEL = Path.home() / ".local/share/dub-models/wespeaker_en_voxceleb_CAM++.onnx"
VOICEVOX = "http://127.0.0.1:50021"
SR = 16000
# 候補の声（番号: 名前）。高さ・年代がばらけるように
MALE = {13: "青山龍星", 11: "玄野武宏", 12: "白上虎太郎", 21: "剣崎雌雄", 52: "雀松朱司", 53: "麒ヶ島宗麟",
        42: "ちび式じい", 51: "†聖騎士 紅桜†", 99: "離途", 100: "黒沢冴白"}
FEMALE = {2: "四国めたん", 8: "春日部つむぎ", 14: "冥鳴ひまり", 10: "雨晴はう", 9: "波音リツ", 3: "ずんだもん",
          16: "九州そら", 20: "もち子さん", 23: "WhiteCUL", 46: "小夜/SAYO"}
SAMPLE = "こんにちは。今日はいい天気ですね。ちょっと聞いてほしいことがあるんだけど、いいかな。"


def f0_median(x):
    """声の高さ（Hz）の真ん中の値。40ms ごとの自己相関で、声らしいところだけ数える（70〜400Hz）"""
    win, hop = int(SR * 0.04), int(SR * 0.02)
    lo, hi = int(SR / 400), int(SR / 70)
    vals = []
    for s in range(0, len(x) - win, hop):
        fr = x[s:s + win] - np.mean(x[s:s + win])
        e = np.dot(fr, fr)
        if e < 1e-4:
            continue
        ac = np.correlate(fr, fr, "full")[win - 1:]
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] / e > 0.55:
            vals.append(SR / k)
    return float(np.median(vals)) if len(vals) >= 5 else None


def embed(ex, x):
    s = ex.create_stream()
    s.accept_waveform(SR, x)
    s.input_finished()
    e = np.array(ex.compute(s))
    return e / np.linalg.norm(e)


def voicevox_sample(sp):
    q = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{VOICEVOX}/audio_query?speaker={sp}&text={quote(SAMPLE)}", method="POST"), timeout=60).read())
    wav = urllib.request.urlopen(urllib.request.Request(
        f"{VOICEVOX}/synthesis?speaker={sp}", data=json.dumps(q).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=120).read()
    x, sr = sf.read(io.BytesIO(wav), dtype="float32")
    return np.interp(np.arange(0, len(x), sr / SR), np.arange(len(x)), x).astype("float32")  # 16kHz に


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--src", default="opus")
    ap.add_argument("--out", default="fit")
    a = ap.parse_args()
    src = D / (f"{a.video_id}.{a.src}.script.json" if a.src else f"{a.video_id}.script.json")
    d = json.loads(src.read_text())
    lines = d["lines"]
    video = next(f for f in D.glob(f"{a.video_id}.video.*") if ".part" not in f.name)
    audio = np.frombuffer(subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar",
                                          str(SR), "-f", "f32le", "-"], capture_output=True).stdout, dtype="float32")
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=4))

    # 登場人物ごとに、全部のセリフの区間をつないで測る
    segs, sexes = defaultdict(list), {}
    for n, L in enumerate(lines):
        b = min(L["end"], lines[n + 1]["t"] if n + 1 < len(lines) else L["end"])
        if b - L["t"] >= 0.5:
            segs[L["who"]].append(audio[int(L["t"] * SR):int(b * SR)])
        sexes[L["who"]] = L.get("sp", "M")
    chars = {}
    for w, ss in segs.items():
        x = np.concatenate(ss)
        chars[w] = {"f0": f0_median(x), "emb": embed(ex, x), "sec": len(x) / SR, "sex": sexes[w]}
        print(f"{w}: {chars[w]['sec']:.0f} 秒 高さ {chars[w]['f0'] or 0:.0f}Hz", file=sys.stderr)

    # VOICEVOX の声を測る
    refs = {}
    for sp, name in {**MALE, **FEMALE}.items():
        x = voicevox_sample(sp)
        refs[sp] = {"name": name, "f0": f0_median(x), "emb": embed(ex, x), "sex": "F" if sp in FEMALE else "M"}
    print("VOICEVOX: " + "、".join(f"{r['name']} {r['f0'] or 0:.0f}Hz" for r in refs.values()), file=sys.stderr)

    # セリフの多い人から、重ならないように近い声を選ぶ（高さの差（半音）＋声の特徴の違い）
    used, cast = set(), {}
    for w in sorted(chars, key=lambda w: -chars[w]["sec"]):
        c = chars[w]
        best = None
        for sp, r in refs.items():
            if r["sex"] != c["sex"] or sp in used:
                continue
            semis = abs(12 * np.log2(c["f0"] / r["f0"])) if c["f0"] and r["f0"] else 6
            score = semis + 8 * (1 - float(c["emb"] @ r["emb"]))
            if best is None or score < best[0]:
                best = (score, sp, semis)
        if best:
            used.add(best[1])
            cast[w] = [best[1], 0.0]
            print(f"{w}（{c['f0'] or 0:.0f}Hz）→ {refs[best[1]]['name']}（{refs[best[1]]['f0'] or 0:.0f}Hz、差 {best[2]:.1f} 半音）")
    for L in lines:
        if L["who"] in cast:
            L["voice"] = cast[L["who"]]
    out = D / f"{a.video_id}.{a.out}.script.json"
    out.write_text(json.dumps({**d, "cast": cast, "variant": f"{a.src}+声選び", "lines": lines}, ensure_ascii=False, indent=1))
    print(f"書いた: {out}")


if __name__ == "__main__":
    main()
