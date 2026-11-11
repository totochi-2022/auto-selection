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
import base64
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
CHANNEL_N = 40  # 「このチャンネル」モードで取る本数（最新分・チャンネル内検索それぞれ）
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
CHANNEL_FILTER = "EgIQAg%3D%3D"  # YouTube 検索の「チャンネル」絞り込み
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


def plan_search(request):
    """希望から探し方を決めてもらう。
    戻り値 {"kind": "music"|"channel"|"topic", "queries": [...], "live": bool, "channel": チャンネル名}"""
    obj = ask_claude(f"""次の希望に合う YouTube 動画を探します。まず、希望がどの種類かを判断してください。
- "music"：曲名・歌手名・アーティスト名・アルバム名など、音楽を聴きたい希望
- "channel"：YouTube のチャンネル名や、特定の YouTuber・番組・公式アカウントの動画を観たい希望
- "topic"：上のどちらでもない、分野・テーマ・作品・人物などの希望

次に、検索ワードを3個考えてください。希望の内容から外れないことが最優先です。
- music：曲そのものが聴ける言葉で探す（正しい曲名・アーティスト名、英語や原題の表記、「MV」「ライブ」「演奏」「cover」など）。
  解説・リアクションになる言葉は足さない。
- channel：チャンネル内で探すための短い言葉（そのチャンネルの定番シリーズ名など。なければ希望の言葉）。
- topic：曲名・作品名・人名など特定のものなら余計な言葉は足さず、正しい表記や「そのもの」が観られる言葉で探す。
  分野やテーマなら、同じような動画ばかりにならないよう切り口を変える（解説・実況・ドキュメンタリー・講座など）。
音声入力の聞き間違いや表記ゆれは、正しい表記に直してください。

さらに、希望が「単語（曲名・人名・作品名・チャンネル名などの短い言葉）」か「文章（条件や雰囲気を説明している）」かを
判断してください（keyword）。単語なら、その言葉そのものでも検索します。

希望に条件が含まれていれば、絞り込み（filters）にしてください。含まれていなければ空にします。
- sort：並べ方。"date"（新しい順）/ "views"（再生数の多い順）/ ""（指定なし）
- period：アップロード日。"hour" / "today" / "week" / "month" / "year" / ""（指定なし）
- min_views / max_views：再生数の下限・上限（整数）。指定がなければ null
- min_minutes / max_minutes：長さの下限・上限（分）。指定がなければ null
例：「新しい将棋の動画で再生数 100 回以内」→ sort "date"、period "today"、max_views 100、
   検索ワードは「将棋」のような短く広い言葉にする（再生数の少ない動画は、今日アップロードされたものを
   新しい順に見ないとほとんど見つからない。言葉を足すと 0 件になりやすい）

# 希望
{request}

また、希望が「今配信中のライブ・生配信・生中継」を観たいものかどうかも判断してください
（過去の中継の解説やアーカイブ、音楽のライブ映像を観たいだけなら false）。

次の JSON だけを1行で出力してください:
{{"kind": "music" または "channel" または "topic", "channel": "channel のときだけチャンネル名（正しい表記）、それ以外は空", "keyword": true または false, "queries": ["検索ワード1", "検索ワード2", "検索ワード3"], "live": true または false, "filters": {{"sort": "", "period": "", "min_views": null, "max_views": null, "min_minutes": null, "max_minutes": null}}}}""")
    obj = obj or {}
    qs = [q for q in obj.get("queries", []) if isinstance(q, str) and q.strip()][:3]
    # 単語の希望は、その言葉そのものでも探す（YouTube は単独なら表記の間違いを補正してくれるが、
    # 言葉を足すと補正されず 0 件になる）。文章の希望は Claude が直した検索ワードだけを使う
    if (obj.get("keyword") is not False or not qs) and len(request) <= 40 and request not in qs:
        qs.insert(0, request)
    kind = obj.get("kind") if obj.get("kind") in ("music", "channel", "topic") else "topic"
    channel = str(obj.get("channel") or "").strip()
    if kind == "channel" and not channel:
        kind = "topic"
    return {"kind": kind, "queries": qs or [request], "live": obj.get("live") is True, "channel": channel,
            "filters": clean_filters(obj.get("filters"))}


def clean_filters(f):
    """Claude が返した絞り込みを確かめて整える（おかしな値は捨てる）"""
    f = f if isinstance(f, dict) else {}
    out = {}
    if f.get("sort") in ("date", "views"):
        out["sort"] = f["sort"]
    if f.get("period") in PERIODS:
        out["period"] = f["period"]
    for k in ("min_views", "max_views", "min_minutes", "max_minutes"):
        try:
            v = int(f.get(k))
            if v >= 0:
                out[k] = v
        except (TypeError, ValueError):
            pass
    return out


def describe_filters(f):
    """絞り込みを短い日本語にする（リモコンの表示用）"""
    parts = []
    if f.get("sort"):
        parts.append({"date": "新しい順", "views": "再生数順"}[f["sort"]])
    if f.get("period"):
        parts.append({"hour": "1時間以内", "today": "今日", "week": "今週", "month": "今月", "year": "今年"}[f["period"]])
    if "min_views" in f or "max_views" in f:
        parts.append(f"再生数 {f.get('min_views', 0):,}〜{'' if 'max_views' not in f else format(f['max_views'], ',')}回")
    if "min_minutes" in f or "max_minutes" in f:
        parts.append(f"長さ {f.get('min_minutes', '')}〜{f.get('max_minutes', '')}分")
    return "・".join(parts)


PERIODS = {"hour": 1, "today": 2, "week": 3, "month": 4, "year": 5}


def search_params(sort="", period="", live=False):
    """YouTube 検索の絞り込み（URL の sp=）を作る。中身は protobuf：
    1=並べ方（2 新しい順、3 再生数順）、2=フィルタ { 1=アップロード日, 8=ライブ }"""
    inner = b""
    if period in PERIODS:
        inner += bytes([0x08, PERIODS[period]])
    if live:
        inner += b"\x40\x01"
    out = b""
    if sort in ("date", "views"):
        out += bytes([0x08, {"date": 2, "views": 3}[sort]])
    if inner:
        out += bytes([0x12, len(inner)]) + inner
    return urllib.parse.quote(base64.b64encode(out).decode()) if out else ""


def make_queries(request):
    """（コマンド版用）戻り値 (検索ワード, ライブ希望か)"""
    plan = plan_search(request)
    return plan["queries"], plan["live"]


def find_channel(name):
    """チャンネル名から YouTube のチャンネルを探す。戻り値 {"id", "name"} か None"""
    r = subprocess.run(
        ["yt-dlp", "--extractor-args", "youtube:lang=ja", "--flat-playlist", "-J", "--playlist-end", "1",
         SEARCH_URL + urllib.parse.quote_plus(name) + "&sp=" + CHANNEL_FILTER],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        log(f"チャンネルの検索に失敗: {r.stderr.strip()[-300:]}")
        return None
    for e in json.loads(r.stdout).get("entries") or []:
        cid = e.get("channel_id") or e.get("id")
        if cid and cid.startswith("UC"):
            return {"id": cid, "name": e.get("channel") or e.get("title") or name}
    return None


def search(query, live=False, long=False, newest=False, filters=None):
    """YouTube を検索する。
    live=True: 配信中のライブだけ / long=True: 10 時間超も含める / newest=True: 新しい順
    filters: plan_search の絞り込み（並べ方・アップロード日は YouTube 側で、再生数・長さは結果を見て絞る）"""
    f = filters or {}
    q = urllib.parse.quote_plus(query)
    sort = "date" if newest else f.get("sort", "")
    sp = search_params(sort=sort, period=f.get("period", ""), live=live)
    # 再生数・長さで絞るときは、残る本数が減るので多めに取る
    n = N_PER_QUERY * (3 if any(k in f for k in ("min_views", "max_views", "min_minutes", "max_minutes")) else 1)
    if sp:
        target = [SEARCH_URL + q + "&sp=" + sp, "--playlist-end", str(n)]
    else:
        target = [f"ytsearch{n}:{query}"]
    r = subprocess.run(
        ["yt-dlp", "--extractor-args", "youtube:lang=ja",  # 付けないとタイトルが英訳されることがある
         "--flat-playlist", "-J", *target],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp の検索に失敗: {r.stderr.strip()[-300:]}")
    entries = json.loads(r.stdout).get("entries") or []
    # 再生数の少ない動画が見つからなければ、今日アップロードされたものに絞ってもう一度だけ探す
    if ("max_views" in f and f.get("period") not in ("hour", "today")
            and not any(passes(e, f) for e in entries)):
        log(f"再生数 {f['max_views']} 回以下が見つからないので、今日の動画で探し直します")
        return search(query, live=live, long=long, newest=True, filters={**f, "period": "today"})
    if live:
        return [e for e in entries if e.get("id") and e.get("live_status") == "is_live" and passes(e, f, live=True)]
    return [e for e in entries if e.get("id") and e.get("duration")
            and MIN_SEC <= e["duration"] and (long or e["duration"] <= MAX_SEC) and passes(e, f)]


def passes(e, f, live=False):
    """再生数・長さの絞り込みに合うか（分からない値は通す）"""
    v = e.get("view_count")
    if v is not None:
        if v < f.get("min_views", 0) or ("max_views" in f and v > f["max_views"]):
            return False
    d = e.get("duration")
    if d and not live:
        if d < f.get("min_minutes", 0) * 60 or ("max_minutes" in f and d > f["max_minutes"] * 60):
            return False
    return True


def gather(queries, *, normal=True, live=False, long=False, newest=False, exclude=(), filters=None):
    """検索ワードごとに検索して、候補をまとめる（exclude の id と重複は除く）"""
    cands, ids = [], set(exclude)
    modes = ([False] if normal else []) + ([True] if live else [])
    for q in queries:
        for is_live in modes:
            log(f"「{q}」を検索中…" + ("（ライブ）" if is_live else ""))
            try:
                found = search(q, live=is_live, long=long, newest=newest, filters=filters)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                log(str(e))
                continue
            for c in found:
                if c["id"] not in ids:
                    ids.add(c["id"])
                    cands.append(c)
    return cands


def channel_videos(channel_id, channel, queries=(), *, long=False, exclude=()):
    """チャンネルの動画を集める：新しい順の最新分＋チャンネル内で検索ワードを検索した結果"""
    base = f"https://www.youtube.com/channel/{channel_id}"
    targets = [(base + "/videos", "")] + [(base + "/search?query=" + urllib.parse.quote_plus(q), f"（{q}）")
                                          for q in queries]
    cands, ids = [], set(exclude)
    for url, note in targets:
        log(f"チャンネル「{channel}」の動画を取得中…{note}")
        r = subprocess.run(
            ["yt-dlp", "--extractor-args", "youtube:lang=ja", "--flat-playlist", "-J",
             "--playlist-end", str(CHANNEL_N), url],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            log(f"チャンネルの取得に失敗: {r.stderr.strip()[-300:]}")
            continue
        for e in json.loads(r.stdout).get("entries") or []:
            d = e.get("duration")
            if not e.get("id") or e["id"] in ids or not d or d < MIN_SEC or (not long and d > MAX_SEC):
                continue
            ids.add(e["id"])
            e["channel"], e["channel_id"] = channel, channel_id  # 一覧には入っていないので補う
            cands.append(e)
    return cands


def fmt_dur(sec):
    if not sec:
        return "ライブ配信中"
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def choose(request, candidates, history, n=1, newest=False, channel=None, kind="topic"):
    """Claude に n 本選ばせる（再生する順）。戻り値 [(候補, 理由), ...]"""
    n = min(n, len(candidates))
    recent = [h for h in history if h.get("event") == "played"][-10:]
    rated = [h for h in history if h.get("event") in ("good", "bad")][-30:]
    lines = [f"{i}. {c['title']} ／ {c.get('channel') or '?'} ／ {fmt_dur(c.get('duration'))}"
             f" ／ {c.get('view_count') or '?'}回"
             for i, c in enumerate(candidates)]
    newest_rule = ("\n4. 新しい動画を優先する（候補は検索ワードごとに新しい順に並んでいる）"
                   if newest else "")
    rule3 = ("3. 候補はすべてユーザーが気に入ったチャンネル「" + channel + "」の動画。"
             "希望に合うものを優先し、足りなければこのチャンネルの中で面白そうなものを選ぶ。"
             "最近観た動画や、選んだ動画どうしで同じ内容のものは避ける"
             if channel else
             "3. 最近観た動画や、選んだ動画どうしで似すぎるもの（同じチャンネルが続く・同じ内容）は避ける")
    if kind == "music" and not channel:
        rule3 += ("\n4. 音楽を聴きたい希望なので、曲そのものが聴ける動画（公式 MV・ライブ演奏・カバー・演奏してみた）を選ぶ。"
                  "解説・リアクション・切り抜き・ランキング紹介は避ける。同じ曲の別の演奏や、同じアーティストの別の曲は可")
    obj = ask_claude(f"""YouTube の動画を{n}本選んでください。テレビで流しっぱなしにして、選んだ順に観ます。

# 今回の希望（最優先）
{request}

# 選び方
1. 今回の希望に合うものを選ぶ。これが最優先
2. 好みメモ・評価・最近観た動画は「参考」。希望に合う候補の中で迷ったときだけ使う
   （ユーザーはいつもと毛色の違う動画も観たいので、過去の傾向に寄せすぎない）
{rule3}{newest_rule}

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
