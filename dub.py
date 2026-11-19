#!/usr/bin/env python3
"""英語の動画をずんだもんたちの日本語の声で吹き替える（試作）。

    python3 dub.py VIDEO_ID [--max-sec 180] [--play]

1. 英語の字幕（無ければ自動字幕）を時刻付きで取る（yt-dlp、json3）
2. Claude で日本語に訳す。字幕の行を自然な文にまとめ、話しているのが男性か女性かも決めてもらう
3. VOICEVOX で 1 文ずつ声にする（女性＝ずんだもん、男性＝青山龍星）。次の文までに収まらなければ速く読む
4. 時刻どおりに並べた 1 本の WAV（~/.cache/autotube/dub/VIDEO_ID.ja.wav）にする
--play なら mpv で、元の音を小さくして重ねて流す
"""
import argparse
import array
import io
import json
import subprocess
import sys
import tempfile
import urllib.request
import wave
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent))
import autotube as at  # noqa: E402  ask_claude を使う

VOICEVOX = "http://127.0.0.1:50021"
VOICES = {"F": 3, "M": 13}  # ずんだもん（ノーマル）・青山龍星（ノーマル）
RATE = 24000
OUT = Path.home() / ".cache/autotube/dub"
CHUNK = 40  # 1 回に訳す字幕の行数


def log(msg):
    print(f"[dub] {msg}", file=sys.stderr, flush=True)


def fetch_cues(video_id, max_sec):
    """英語の字幕を [{"t": 開始秒, "d": 長さ秒, "text": 英文}] で返す"""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(
            ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
             "--sub-format", "json3", "-o", f"{d}/%(id)s.%(ext)s",
             "--extractor-args", "youtube:player_skip=js,configs", "--ignore-no-formats-error",
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=120,
        )
        files = sorted(Path(d).glob("*.json3"), key=lambda f: ("orig" in f.name, f.name))  # 手作りの字幕を先に
        if not files:
            raise RuntimeError("英語の字幕が見つかりません")
        log(f"字幕: {files[0].name}")
        ev = json.loads(files[0].read_text()).get("events") or []
    cues = []
    for e in ev:
        text = "".join(s.get("utf8", "") for s in (e.get("segs") or [])).replace("\n", " ").strip()
        if not text or e.get("aAppend"):
            continue
        t = e.get("tStartMs", 0) / 1000
        if t > max_sec:
            break
        cues.append({"t": t, "d": e.get("dDurationMs", 0) / 1000, "text": text})
    return cues


def translate(cues):
    """字幕の行を日本語の文にまとめて訳す。[{"i": 最初の行, "j": 最後の行, "sp": "M"|"F", "ja": 訳}]"""
    out = []
    for k in range(0, len(cues), CHUNK):
        part = cues[k:k + CHUNK]
        lines = "\n".join(f"[{k + n}] ({c['t']:.1f}s) {c['text']}" for n, c in enumerate(part))
        prev = out[-1]["ja"] if out else "(なし)"
        obj = at.ask_claude(f"""英語の動画の字幕を、日本語の吹き替えの台本にしてください。

- 字幕の行は途中で切れているので、続いている行をまとめて、自然な日本語の文にする（1 文はだいたい 2〜4 行分）
- 吹き替えなので話し言葉で、元の字幕の時間に収まるよう短めに訳す（意味は落とさない）
- ">>" は話す人が変わる印。話しているのが男性（M）か女性（F）かを、印と話の流れで決める（分からなければ前の文と同じ）
- 番号は抜けや重なりなく、[{k}] から [{k + len(part) - 1}] まで全部をどれかの文に入れる

直前の文（参考）: {prev}

字幕:
{lines}

次の JSON だけを出力してください（説明は不要）:
{{"s": [{{"i": 最初の行の番号, "j": 最後の行の番号, "sp": "M" または "F", "ja": "日本語"}}]}}""")
        got = [x for x in (obj or {}).get("s") or [] if isinstance(x, dict) and x.get("ja")]
        log(f"訳した: {k}〜{k + len(part) - 1} 行 → {len(got)} 文")
        out += got
    return out


def tts(text, speaker, speed):
    q = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{VOICEVOX}/audio_query?speaker={speaker}&text={quote(text)}", method="POST"), timeout=60).read())
    q["speedScale"] = speed
    q["prePhonemeLength"] = q["postPhonemeLength"] = 0.05
    wav = urllib.request.urlopen(urllib.request.Request(
        f"{VOICEVOX}/synthesis?speaker={speaker}", data=json.dumps(q).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=120).read()
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return array.array("h", w.readframes(w.getnframes()))


def build(video_id, cues, script):
    """訳を時刻どおりに並べた WAV を作る"""
    starts = []
    for s in script:
        try:
            i, j = int(s["i"]), int(s.get("j", s["i"]))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(cues):
            starts.append((cues[i]["t"], cues[min(j, len(cues) - 1)], s))
    starts.sort(key=lambda x: x[0])
    total = int((cues[-1]["t"] + cues[-1]["d"] + 5) * RATE)
    track = array.array("h", bytes(2 * total))
    for n, (t0, last, s) in enumerate(starts):
        nxt = starts[n + 1][0] if n + 1 < len(starts) else last["t"] + last["d"] + 3
        room = max(0.8, nxt - t0)
        voice = VOICES.get(s.get("sp"), VOICES["F"])
        speed = 1.15
        pcm = tts(s["ja"], voice, speed)
        if len(pcm) / RATE > room:  # 次の文までに収まらなければ速く読み直す（2 倍まで）
            speed = min(2.0, speed * len(pcm) / RATE / room * 1.05)
            pcm = tts(s["ja"], voice, speed)
        at_ = int(t0 * RATE)
        for k, v in enumerate(pcm[:max(0, total - at_)]):
            track[at_ + k] = max(-32768, min(32767, track[at_ + k] + v))
        log(f"{t0:6.1f}s {s.get('sp')} x{speed:.2f} {s['ja'][:40]}")
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / f"{video_id}.ja.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(track.tobytes())
    (OUT / f"{video_id}.ja.json").write_text(json.dumps(script, ensure_ascii=False, indent=1))
    return f


def play(video_id, wav, max_sec):
    """元の音を 25% にして吹き替えを重ねて流す"""
    subprocess.run(["mpv", f"https://www.youtube.com/watch?v={video_id}", f"--audio-file={wav}",
                    "--lavfi-complex=[aid1]volume=0.25[a1];[aid2]volume=1.0[a2];[a1][a2]amix=inputs=2:normalize=0[ao]",
                    f"--end={max_sec}", "--ytdl-format=bv*[height<=1080]+ba/b", "--fs"],
                   env=at.mpv_env())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--max-sec", type=float, default=180)
    ap.add_argument("--play", action="store_true")
    a = ap.parse_args()
    cues = fetch_cues(a.video_id, a.max_sec)
    log(f"字幕 {len(cues)} 行（{a.max_sec:.0f} 秒まで）")
    script = translate(cues)
    wav = build(a.video_id, cues, script)
    log(f"できた: {wav}")
    if a.play:
        play(a.video_id, wav, a.max_sec)


if __name__ == "__main__":
    main()
