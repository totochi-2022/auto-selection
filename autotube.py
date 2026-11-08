#!/usr/bin/env python3
"""分野を指定すると、Claude が YouTube 動画を選んで mpv で流し続ける。

使い方:
  autotube.py 将棋                          # 選んで再生、終わったら次を選ぶ
  autotube.py 将棋 いつもと違う振り飛車の講座  # 希望は文章でもよい
  autotube.py 将棋 ライブ中継                # ライブ希望なら配信中のものから探す
  autotube.py 猫 --live                     # 配信中のものから探す（強制）
  autotube.py 将棋 --dry-run                # 選ぶだけ（再生しない）

優先順位: 今回の希望 ＞ 好みメモ・評価・履歴（希望の範囲内で迷ったときの参考）

mpv のキー: n=次へ  q=終了  g=👍  b=👎
（Web 画面から操作したいときは web.py を使う）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent
PREFS = BASE / "prefs.md"
DATA = BASE / "data"
HISTORY = DATA / "history.jsonl"
INPUT_CONF = BASE / "mpv-input.conf"

N_PER_QUERY = 15
MIN_SEC, MAX_SEC = 120, 10 * 3600  # ショートと極端に長いものを除く
# HLS（m3u8）を優先する。通常の DASH 形式は mpv(ffmpeg) が直接取りに行くと 403 で拒否されることがある
def ytdl_format(max_h=1080):
    return (f"bv[protocol^=m3u8][height<={max_h}][vcodec^=avc1]+ba[protocol^=m3u8]"
            f"/b[protocol^=m3u8][height<={max_h}]"
            f"/bv[height<={max_h}][vcodec^=avc1]+ba/b[height<={max_h}]/b")


YTDL_FORMAT = ytdl_format(1080)
SEARCH_URL = "https://www.youtube.com/results?search_query="
LIVE_FILTER = "EgJAAQ%3D%3D"  # YouTube 検索の「ライブ」絞り込み
DATE_SORT = "CAI%3D"          # YouTube 検索の「アップロード日（新しい順）」
CLAUDE_MODEL = os.environ.get("AUTOTUBE_MODEL", "haiku")
QUIT_STOP = 4  # mpv-input.conf の q
IS_WSL = bool(os.environ.get("WSL_DISTRO_NAME"))


def log(msg):
    print(f"[autotube] {msg}", file=sys.stderr, flush=True)


def load_history():
    if not HISTORY.exists():
        return []
    out = []
    for line in HISTORY.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def append_history(rec):
    DATA.mkdir(exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ensure_prefs():
    DATA.mkdir(exist_ok=True)
    if not PREFS.exists() and (BASE / "prefs.example.md").exists():
        shutil.copy(BASE / "prefs.example.md", PREFS)  # 好みメモは個人用なので git 管理外


def ask_claude(prompt):
    """claude -p に聞いて、返事の中の JSON を取り出す"""
    r = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--tools", "",
         "--no-session-persistence", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=180, cwd=DATA,
    )
    text = r.stdout.strip()
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        log(f"Claude の返事を読めませんでした: {text[:200]!r} {r.stderr[-200:]!r}")
        return None


def make_queries(request):
    """希望から YouTube の検索ワードを考えてもらう。戻り値 (検索ワード, ライブ希望か)"""
    obj = ask_claude(f"""次の希望に合う YouTube 動画を探すための検索ワードを3個考えてください。
同じような動画ばかりにならないよう、切り口を変えてください（例：解説・実況・ドキュメンタリー・講座など）。
希望の内容から外れないことが最優先です。

# 希望
{request}

また、希望が「今配信中のライブ・生配信・生中継」を観たいものかどうかも判断してください
（過去の中継の解説やアーカイブを観たいだけなら false）。

次の JSON だけを1行で出力してください:
{{"queries": ["検索ワード1", "検索ワード2", "検索ワード3"], "live": true または false}}""")
    obj = obj or {}
    qs = [q for q in obj.get("queries", []) if isinstance(q, str) and q.strip()]
    return qs[:3] or [request], obj.get("live") is True


def search(query, live=False, long=False, newest=False):
    """YouTube を検索する。
    live=True: 配信中のライブだけ / long=True: 10 時間超も含める / newest=True: 新しい順"""
    q = urllib.parse.quote_plus(query)
    if live:
        target = [SEARCH_URL + q + "&sp=" + LIVE_FILTER, "--playlist-end", str(N_PER_QUERY)]
    elif newest:
        target = [SEARCH_URL + q + "&sp=" + DATE_SORT, "--playlist-end", str(N_PER_QUERY)]
    else:
        target = [f"ytsearch{N_PER_QUERY}:{query}"]
    r = subprocess.run(
        ["yt-dlp", "--extractor-args", "youtube:lang=ja",  # 付けないとタイトルが英訳されることがある
         "--flat-playlist", "-J", *target],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp の検索に失敗: {r.stderr.strip()[-300:]}")
    entries = json.loads(r.stdout).get("entries") or []
    if live:
        return [e for e in entries if e.get("id") and e.get("live_status") == "is_live"]
    return [e for e in entries if e.get("id") and e.get("duration")
            and MIN_SEC <= e["duration"] and (long or e["duration"] <= MAX_SEC)]


def gather(queries, *, normal=True, live=False, long=False, newest=False, exclude=()):
    """検索ワードごとに検索して、候補をまとめる（exclude の id と重複は除く）"""
    cands, ids = [], set(exclude)
    modes = ([False] if normal else []) + ([True] if live else [])
    for q in queries:
        for is_live in modes:
            log(f"「{q}」を検索中…" + ("（ライブ）" if is_live else ""))
            try:
                found = search(q, live=is_live, long=long, newest=newest)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                log(str(e))
                continue
            for c in found:
                if c["id"] not in ids:
                    ids.add(c["id"])
                    cands.append(c)
    return cands


def fmt_dur(sec):
    if not sec:
        return "ライブ配信中"
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def choose(request, candidates, history, n=1, newest=False):
    """Claude に n 本選ばせる（再生する順）。戻り値 [(候補, 理由), ...]"""
    n = min(n, len(candidates))
    recent = [h for h in history if h.get("event") == "played"][-10:]
    rated = [h for h in history if h.get("event") in ("good", "bad")][-30:]
    lines = [f"{i}. {c['title']} ／ {c.get('channel') or '?'} ／ {fmt_dur(c.get('duration'))}"
             f" ／ {c.get('view_count') or '?'}回"
             for i, c in enumerate(candidates)]
    newest_rule = ("\n4. 新しい動画を優先する（候補は検索ワードごとに新しい順に並んでいる）"
                   if newest else "")
    obj = ask_claude(f"""YouTube の動画を{n}本選んでください。テレビで流しっぱなしにして、選んだ順に観ます。

# 今回の希望（最優先）
{request}

# 選び方
1. 今回の希望に合うものを選ぶ。これが最優先
2. 好みメモ・評価・最近観た動画は「参考」。希望に合う候補の中で迷ったときだけ使う
   （ユーザーはいつもと毛色の違う動画も観たいので、過去の傾向に寄せすぎない）
3. 最近観た動画や、選んだ動画どうしで似すぎるもの（同じチャンネルが続く・同じ内容）は避ける{newest_rule}

# 参考：好みメモ
{PREFS.read_text() if PREFS.exists() else '(なし)'}

# 参考：評価（good=好き、bad=いまいち）
{chr(10).join(f"- {h['event']}: {h['title']} ／ {h.get('channel')}" for h in rated) or '(まだなし)'}

# 参考：最近観た動画
{chr(10).join(f"- {h['title']} ／ {h.get('channel')}" for h in recent) or '(まだなし)'}

# 候補（番号. タイトル ／ チャンネル ／ 長さ ／ 再生数）
{chr(10).join(lines)}

次の JSON だけを1行で出力してください（説明文やコードブロックは不要）。picks は観る順に{n}個:
{{"picks": [{{"index": 候補の番号, "reason": "選んだ理由を日本語で短く"}}]}}""")
    picks, used = [], set()
    for p in (obj or {}).get("picks") or []:
        try:
            i = int(p["index"])
        except (TypeError, ValueError, KeyError):
            continue
        if 0 <= i < len(candidates) and i not in used:
            used.add(i)
            picks.append((candidates[i], str(p.get("reason", ""))))
    if not picks:
        log("選べなかったので先頭の候補にします")
        picks = [(c, "(自動で選択)") for c in candidates[:n]]
    return picks[:n]


def pick_next(request, queries, live):
    history = load_history()
    seen = {h["id"] for h in history if h.get("event") == "played"}
    cands = gather(queries, normal=not live, live=live, exclude=seen)
    if not cands:
        raise RuntimeError("観ていない候補が見つかりませんでした")
    log(f"候補 {len(cands)} 本から Claude が選んでいます…")
    return choose(request, cands, history)[0]


def mpv_env():
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")  # SSH や tmux から起動したとき用
    return env


# WSLg 用の mpv オプション
# - profile=fast: GPU を使わず CPU で描画するため、既定の拡大処理だとフレームの 6 割を捨てる
# - force-render: 最小化して戻したことが mpv に伝わらず、描画を止めたままになる
WSL_MPV_ARGS = ["--profile=fast", "--force-render"]


def mpv_screen_args():
    # WSLg では --fs だとウィンドウが画面外に置かれて見えないため、枠なしの画面サイズで代用する
    if IS_WSL:
        return [*WSL_MPV_ARGS, "--no-border", "--geometry=100%x100%+0+0"]
    return ["--fs"]


def play(video, request):
    """再生して、mpv の終了コードを返す。g/b の評価は履歴に記録"""
    url = f"https://www.youtube.com/watch?v={video['id']}"
    cmd = ["mpv", *mpv_screen_args(), f"--ytdl-format={YTDL_FORMAT}",
           f"--input-conf={INPUT_CONF}", "--msg-level=all=no,cplayer=info",
           f"--force-media-title={video['title']}", url]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, env=mpv_env())
    for line in p.stdout:
        line = line.strip()
        if line.startswith("AUTOTUBE:"):
            ev = line.split(":", 1)[1]
            append_history({"event": ev, "ts": int(time.time()), "id": video["id"],
                            "title": video["title"], "channel": video.get("channel"),
                            "request": request})
            log(f"{'👍' if ev == 'good' else '👎'} を記録しました")
    return p.wait()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request", nargs="+", help="分野や希望（例: 将棋 いつもと違う振り飛車の講座）")
    ap.add_argument("--live", action="store_true", help="配信中のライブから探す")
    ap.add_argument("--dry-run", action="store_true", help="選ぶだけで再生しない")
    args = ap.parse_args()
    ensure_prefs()
    request = " ".join(args.request)
    log("検索ワードを考えています…")
    queries, live = make_queries(request)
    live = live or args.live
    log("検索ワード: " + " / ".join(queries) + ("（配信中のライブから）" if live else ""))

    while True:
        video, reason = pick_next(request, queries, live)
        log(f"▶ {video['title']} ／ {video.get('channel')} ／ {fmt_dur(video.get('duration'))}")
        log(f"  理由: {reason}")
        if args.dry_run:
            return
        append_history({"event": "played", "ts": int(time.time()), "id": video["id"],
                        "title": video["title"], "channel": video.get("channel"),
                        "request": request, "reason": reason})
        code = play(video, request)
        if code == QUIT_STOP:
            log("終了します")
            return
        if code not in (0,):
            log(f"mpv が異常終了しました (code={code})。次へ進みます")
            time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        log(str(e))
        sys.exit(1)
