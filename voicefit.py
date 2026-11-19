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
MAX_VOICES = 5  # 1 本の動画で使う声の種類の上限（メモリ）
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


def restart_voicevox():
    import time
    subprocess.run(["systemctl", "--user", "restart", "voicevox"])
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{VOICEVOX}/version", timeout=2).read()
            return
        except OSError:
            time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--src", default="opus")
    ap.add_argument("--out", default="fit")
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).parent))
    import dub
    src = dub.script_path(a.video_id, a.src)
    d = json.loads(src.read_text())
    lines = d["lines"]
    for L in lines:  # 配役を丁寧に決めていない台本（普通の動画）：男女だけで「男性」「女性」の 2 人とみなす
        if not L.get("who"):
            L["who"] = "女性" if L.get("sp") == "F" else "男性"
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

    # VOICEVOX の声を測る（一度測ったら REFS に保存して使い回す）。VOICEVOX は使った声を読み込んだままにするので、
    # 20 種類を続けて使うとメモリが 4GB を超えて止められる → 7 種類ごとに VOICEVOX を起動し直してメモリを空ける
    REFS = Path.home() / ".local/share/dub-models/voicevox-refs.json"
    saved = json.loads(REFS.read_text()) if REFS.exists() else {}
    refs, fresh = {}, 0
    for sp, name in {**MALE, **FEMALE}.items():
        if str(sp) not in saved:
            if fresh and fresh % 7 == 0:
                restart_voicevox()
            x = voicevox_sample(sp)
            saved[str(sp)] = {"f0": f0_median(x), "emb": embed(ex, x).tolist()}
            REFS.write_text(json.dumps(saved))
            fresh += 1
        r = saved[str(sp)]
        refs[sp] = {"name": name, "f0": r["f0"], "emb": np.array(r["emb"]), "sex": "F" if sp in FEMALE else "M"}
    if fresh:
        restart_voicevox()  # 読み込んだ声を手放す
    print("VOICEVOX: " + "、".join(f"{r['name']} {r['f0'] or 0:.0f}Hz" for r in refs.values()), file=sys.stderr)

    # 高さは「順番合わせ」：声優・VOICEVOX は男はより低く女はより高く作ってあるので、Hz をそのまま比べず、
    # 男女それぞれで、登場人物の高さの幅を VOICEVOX の声の高さの幅に縮めて当てはめる（高い人は高い声、低い人は低い声）
    def target(c):
        cs = [x["f0"] for x in chars.values() if x["sex"] == c["sex"] and x["f0"]]
        rs = [r["f0"] for r in refs.values() if r["sex"] == c["sex"] and r["f0"]]
        if not c["f0"] or len(cs) < 2 or max(cs) - min(cs) < 1:
            return float(np.exp(np.mean(np.log(rs))))  # 1 人だけなら真ん中の高さ
        k = (np.log(c["f0"]) - np.log(min(cs))) / (np.log(max(cs)) - np.log(min(cs)))
        return float(np.exp(np.log(min(rs)) + k * (np.log(max(rs)) - np.log(min(rs)))))

    # セリフの多い人から、重ならないように近い声を選ぶ（当てはめた高さとの差（半音）＋声の特徴の違い）
    used, cast = set(), {}
    for w in sorted(chars, key=lambda w: -chars[w]["sec"]):
        c = chars[w]
        tgt = target(c)
        best = None
        for sp, r in refs.items():
            if r["sex"] != c["sex"] or sp in used:
                continue
            semis = abs(12 * np.log2(tgt / r["f0"])) if r["f0"] else 6
            score = semis + 4 * (1 - float(c["emb"] @ r["emb"]))
            if best is None or score < best[0]:
                best = (score, sp, semis)
        if best:
            used.add(best[1])
            cast[w] = [best[1], 0.0]
            print(f"{w}（{c['f0'] or 0:.0f}Hz → 当てはめ {tgt:.0f}Hz）→ {refs[best[1]]['name']}（{refs[best[1]]['f0'] or 0:.0f}Hz）")
    # 使う声の種類は MAX_VOICES まで（VOICEVOX は声 1 種類で 200MB ほど使う）。セリフの多い人から順に声の種類を決め、
    # それ以降の人は、すでに使っている同じ性別の声のうち当てはめた高さに近いものを、高さを変えて使う
    order = sorted(cast, key=lambda w: -chars[w]["sec"])
    nf = sum(1 for w in cast if chars[w]["sex"] == "F")
    nm = len(cast) - nf
    quota = {"F": min(nf, max(1, MAX_VOICES - nm)) if nf else 0}  # 女性がいれば女性の声を最低 1 種類
    quota["M"] = MAX_VOICES - quota["F"]
    kinds = []
    for w in order:
        sp, c = cast[w][0], chars[w]
        if sp in kinds or sum(1 for k in kinds if refs[k]["sex"] == c["sex"]) < quota[c["sex"]]:
            kinds.append(sp) if sp not in kinds else None
            continue
        same = [k for k in kinds if refs[k]["sex"] == c["sex"]] or kinds
        k = min(same, key=lambda k: abs(np.log(target(c) / refs[k]["f0"])) if refs[k]["f0"] else 9)
        n_same = sum(1 for x in order[:order.index(w)] if cast[x][0] == k)
        cast[w] = [k, [0.06, -0.06, 0.12, -0.12][n_same % 4] if n_same else 0.06]
        print(f"  声の種類が {MAX_VOICES} を超えるので {w} → {refs[k]['name']}（高さ {cast[w][1]:+.2f}）")
    for L in lines:
        if L["who"] in cast:
            L["voice"] = cast[L["who"]]
    out = dub.save_script(a.video_id, {**d, "cast": cast, "variant": f"{a.src or 'ふつう'}+声選び", "lines": lines}, a.out)
    print(f"書いた: {out}")


if __name__ == "__main__":
    main()
