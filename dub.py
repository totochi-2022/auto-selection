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


def _subs(video_id, langs, auto):
    """yt-dlp で字幕（json3）を取って events を返す。無ければ None"""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(
            ["yt-dlp", "--skip-download", "--write-subs", *(["--write-auto-subs"] if auto else []),
             "--sub-langs", langs, "--sub-format", "json3", "-o", f"{d}/%(id)s.%(ext)s",
             "--extractor-args", "youtube:player_skip=js,configs", "--ignore-no-formats-error",
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=120,
        )
        files = sorted(Path(d).glob("*.json3"), key=lambda f: ("orig" in f.name, f.name))  # 手作りの字幕を先に
        if not files:
            return None
        log(f"字幕: {files[0].name}")
        return json.loads(files[0].read_text()).get("events") or []


def fetch_cues(video_id, max_sec=None):
    """字幕を [{"t": 開始秒, "d": 長さ秒, "text": 文}] と言語（"ja"＝作った人が用意した日本語の字幕／"en"）で返す。
    日本語の字幕（自動翻訳でないもの）があればそれを使い、無ければ英語（手作り → 自動字幕）"""
    lang, ev = "ja", _subs(video_id, "ja", auto=False)
    if not ev:
        lang, ev = "en", _subs(video_id, "en.*,en", auto=True)
    if not ev:
        raise RuntimeError("字幕が見つかりません")
    cues = []
    for e in ev:
        text = "".join(s.get("utf8", "") for s in (e.get("segs") or [])).replace("\n", " ").strip()
        if not text or e.get("aAppend"):
            continue
        t = e.get("tStartMs", 0) / 1000
        if max_sec and t > max_sec:
            break
        cues.append({"t": t, "d": e.get("dDurationMs", 0) / 1000, "text": text})
    return cues, lang


def translate(cues, lang="en", progress=None):
    """字幕の行を日本語の文にまとめて訳す（日本語の字幕なら訳さずに整える）。[{"i": 最初の行, "j": 最後の行, "sp": "M"|"F", "ja": 訳}]"""
    out, cast = [], {}  # cast：これまでの登場人物 呼び名 -> M/F（分けて訳すので、呼び名がぶれないよう毎回渡す）
    for k in range(0, len(cues), CHUNK):
        part = cues[k:k + CHUNK]
        lines = "\n".join(f"[{k + n}] ({c['t']:.1f}s) {c['text']}" for n, c in enumerate(part))
        prev = out[-1]["ja"] if out else "(なし)"
        known = "、".join(f"{w}（{g}）" for w, g in cast.items()) or "(まだなし)"
        what = ("動画の日本語の字幕を、吹き替えの台本に整えてください（訳し直さない）" if lang == "ja"
                else "英語の動画の字幕を、日本語の吹き替えの台本にしてください")
        obj = at.ask_claude(f"""{what}。

- 字幕の行は途中で切れているので、続いている行をまとめて、自然な日本語の文にする（1 文はだいたい 2〜4 行分）
- ただし ">>" の前と後ろは別の人のセリフなので、">>" をまたいで 1 つの文にまとめない（">>" のある行から新しい文にする）
- 吹き替えなので話し言葉で、元の字幕の時間に収まるよう短めにする（意味は落とさない）
- [Music]・[Applause]・(laughs)・♪ のような音や効果の説明は読まないので、その行は "ja" を空にする
- ">>" は話す人が変わる印。誰が話しているか（who）を、印・呼びかけ・話の流れで決める。
  名前が分かれば名前（カタカナ）、分からなければ「男1」「女1」「ナレーター」のような呼び名。同じ人には必ず同じ呼び名を使う
  （これまでの登場人物の呼び名を優先して使う）。男性（M）か女性（F）かも決める（分からなければ前の文と同じ人）
- 番号は抜けや重なりなく、[{k}] から [{k + len(part) - 1}] まで全部をどれかの文に入れる

これまでの登場人物: {known}
直前の文（参考）: {prev}

字幕:
{lines}

次の JSON だけを出力してください（説明は不要）:
{{"s": [{{"i": 最初の行の番号, "j": 最後の行の番号, "who": "話している人の呼び名", "sp": "M" または "F", "ja": "日本語"}}]}}""")
        got = [x for x in (obj or {}).get("s") or [] if isinstance(x, dict) and speakable(x.get("ja"))]
        for x in got:
            if x.get("who"):
                cast.setdefault(str(x["who"]), x.get("sp", "M"))
        log(f"訳した: {k}〜{k + len(part) - 1} 行 → {len(got)} 文")
        out += got
        if progress:
            progress("translate", min(k + CHUNK, len(cues)), len(cues))
    return out


def speakable(ja):
    """読む文か（空や、（音楽）［拍手］♪ のような音の説明だけのものは読まない）"""
    import re
    t = re.sub(r"[（(［\[【][^）)］\]】]*[）)］\]】]|♪", "", ja or "").strip()
    return bool(t)


CAST_MODEL = "sonnet"  # 配役を決める Claude（台本全体を見て、掛け合いを追う）


def assign_roles(lines):
    """訳し終わった台本全体を見て、配役を決め直す（lines の who・sp を書き換える）。
    lines: [{"t", "who", "sp", "ja", "en"}]。en の ">>" は話す人が変わった印"""
    rows = "\n".join(f"[{n}] {x['t']:.1f}s 仮:{x.get('who') or '?'} | {x['en']} | {x['ja']}" for n, x in enumerate(lines))
    obj = at.ask_claude(f"""映画・動画の吹き替えの配役を決めてください。下は台本全体です（番号・時刻・仮の話者・英語の字幕・日本語訳）。
英語の ">>" は話す人が変わった印です（誰に変わったかは書いていない）。仮の話者は細切れに推測したもので、間違いが多いです。

手順:
1. 場面の切れ目と、それぞれの場面に出ている登場人物を整理する
2. 同じ人の別の呼び方（あだ名・偽名・「男1」など）は 1 人にまとめ、呼び名を決める（名前が分かればカタカナの名前）
3. 文ごとに、誰のセリフかを決める。">>" の位置、呼びかけ（名前を呼ばれた人は話していない）、質問と答えの流れ、
   話し方の癖で判断する。">>" が無いところは、たいてい前の文と同じ人
4. 登場人物ごとに男性（M）か女性（F）かを決める

台本:
{rows}

次の JSON だけを出力してください（説明は不要）。roles は全部の番号について:
{{"cast": {{"呼び名": "M" または "F"}}, "roles": [{{"n": 番号, "who": "呼び名"}}]}}""", model=CAST_MODEL, timeout=600)
    cast = (obj or {}).get("cast") or {}
    got = 0
    for r in (obj or {}).get("roles") or []:
        try:
            n = int(r["n"])
        except (TypeError, ValueError, KeyError):
            continue
        if 0 <= n < len(lines) and r.get("who"):
            lines[n]["who"] = str(r["who"])
            lines[n]["sp"] = cast.get(lines[n]["who"], lines[n].get("sp", "M"))
            got += 1
    fix_self_address(lines, cast)
    log(f"配役を決め直した: {got}/{len(lines)} 文、登場人物 {len(cast)} 人 {cast}")
    return lines


def fix_self_address(lines, cast):
    """自分の名前を呼んでいるセリフ（「セス、よく聞け」をセスが言う）は、直前に話していた別の人のセリフにする"""
    names = [w for w in cast if len(w) >= 2 and not w.startswith(("男", "女"))]
    for n, x in enumerate(lines):
        w = x.get("who")
        if w in names and w in (x.get("ja") or ""):
            prev = next((lines[k]["who"] for k in range(n - 1, -1, -1) if lines[k].get("who") not in (None, w)), None)
            if prev:
                log(f"自分の名前を呼んでいるので {w} → {prev}: {x['ja'][:30]}")
                x["who"], x["sp"] = prev, cast.get(prev, x.get("sp", "M"))


MALE_VOICES = [13, 11, 12, 21, 52, 53]   # 青山龍星・玄野武宏・白上虎太郎・剣崎雌雄・雀松朱司・麒ヶ島宗麟
FEMALE_VOICES = [2, 8, 14, 10, 9, 3]     # 四国めたん・春日部つむぎ・冥鳴ひまり・雨晴はう・波音リツ・ずんだもん


PITCHES = [0.0, -0.06, 0.06]  # 声の高さ（VOICEVOX の pitchScale）。声の種類が足りなくなったら高さを変えて別の人にする


def cast_voices(script):
    """登場人物（who）ごとに、出てきた順に声を割り当てる。{呼び名: [話者番号, 声の高さ]}。
    声の種類を一巡させてから、高さを変えた組み合わせを使う（6 種類 × 3 段階）"""
    voices, nm, nf = {}, 0, 0
    for s in script:
        w = str(s.get("who") or s.get("sp") or "M")
        if w in voices:
            continue
        pool, n = (FEMALE_VOICES, nf) if s.get("sp") == "F" else (MALE_VOICES, nm)
        voices[w] = [pool[n % len(pool)], PITCHES[(n // len(pool)) % len(PITCHES)]]
        if s.get("sp") == "F":
            nf += 1
        else:
            nm += 1
    return voices


def tts(text, speaker, speed, pitch=0.0):
    q = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{VOICEVOX}/audio_query?speaker={speaker}&text={quote(text)}", method="POST"), timeout=60).read())
    q["speedScale"] = speed
    q["pitchScale"] = pitch
    q["prePhonemeLength"] = q["postPhonemeLength"] = 0.05
    wav = urllib.request.urlopen(urllib.request.Request(
        f"{VOICEVOX}/synthesis?speaker={speaker}", data=json.dumps(q).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=120).read()
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return array.array("h", w.readframes(w.getnframes()))


def build(video_id, cues, script, progress=None):
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
        if progress:
            progress("voice", n + 1, len(starts))
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / f"{video_id}.ja.wav"
    tmp = f.with_suffix(".part.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(track.tobytes())
    tmp.replace(f)  # できあがってから名前を付ける（途中のファイルを流さない）
    (OUT / f"{video_id}.ja.json").write_text(json.dumps(script, ensure_ascii=False, indent=1))
    return f


def play(video_id, wav, max_sec):
    """元の音を 25% にして吹き替えを重ねて流す"""
    subprocess.run(["mpv", f"https://www.youtube.com/watch?v={video_id}", f"--audio-file={wav}",
                    "--lavfi-complex=[aid1]volume=0.25[a1];[aid2]volume=1.0[a2];[a1][a2]amix=inputs=2:normalize=0[ao]",
                    *([f"--end={max_sec}"] if max_sec else []), "--ytdl-format=bv*[height<=1080]+ba/b", "--fs"],
                   env=at.mpv_env())


def dub_path(video_id):
    return OUT / f"{video_id}.ja.wav"


def dub_video(video_id, progress=None):
    """動画を最後まで吹き替えて WAV の場所を返す（できていればそれ）。字幕が無ければ RuntimeError。
    progress(段階, 済み, 全体)：段階は translate（訳す）・voice（声にする）"""
    f = dub_path(video_id)
    if f.exists():
        return f
    cues, lang = fetch_cues(video_id)
    log(f"{video_id}: 字幕 {len(cues)} 行（{lang}）")
    script = translate(cues, lang, progress)
    if not script:
        raise RuntimeError("台本を作れませんでした")
    return build(video_id, cues, script, progress)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--max-sec", type=float, default=0)
    ap.add_argument("--play", action="store_true")
    a = ap.parse_args()
    cues, lang = fetch_cues(a.video_id, a.max_sec)
    log(f"字幕 {len(cues)} 行（{lang}）")
    script = translate(cues, lang)
    wav = build(a.video_id, cues, script)
    log(f"できた: {wav}")
    if a.play:
        play(a.video_id, wav, a.max_sec)


if __name__ == "__main__":
    main()
