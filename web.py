#!/usr/bin/env python3
"""autotube の Web リモコン。再生は mpv（テレビ側）、操作はブラウザ（スマホ・PC）から。

使い方:
  python3 web.py            # http://<このマシン>:8766/ を開く
  AUTOTUBE_PORT=9000 python3 web.py

mpv は 1 つを常駐させ、JSON IPC（--input-ipc-server）で動画の切り替え・一時停止・最大化などを行う。
次に流す動画は Claude に 5 本まとめて選んでもらい、キューに積んでおく。
mpv が応答しなくなったら（見張り役が数秒ごとに確認）、強制終了して同じ動画を同じ位置から再開する。
"""
import hashlib
import json
import mimetypes
import re
import os
import secrets
import signal
import socket
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import autotube as at
import dub as dubmod  # 英語の動画の吹き替え（ずんだもんたち）

HOST = os.environ.get("AUTOTUBE_HOST", "0.0.0.0")
PORT = int(os.environ.get("AUTOTUBE_PORT", "8766"))  # 8765 は phone-input が使う
QUEUE_LEN = 5
# WSLg では同じ mpv で動画を切り替えると音声出力で固まることがあるため、動画ごとに mpv を起動し直す
RESTART_PER_VIDEO = at.IS_WSL
WATCH_INTERVAL, WATCH_TIMEOUT = 5, 6  # 見張り役: 確認の間隔と、応答を待つ秒数
STALL_SEC = 20     # 再生位置がこれだけ進まなければ読み込み直す（一時停止中は除く）
STALL_RETRIES = 2  # 読み込み直しても進まなければ次の動画へ
WEB_DIR = at.BASE / "web"
INPUT_CONF = at.BASE / "mpv-input-web.conf"
SOCK = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"autotube-mpv-{os.getuid()}.sock"
MAX_ERRORS = 3  # 再生エラーがこれだけ続いたら止める


def log(msg):
    print(f"[web] {msg}", file=sys.stderr, flush=True)


def load_token():
    """合言葉。phone-input（スマホ入力ページ）が隣にあれば、その token を共有する"""
    candidates = [os.environ.get("AUTOTUBE_TOKEN_FILE"),
                  at.BASE.parent / "phone-input" / "token", Path.home() / "phone-input" / "token"]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c).read_text().strip()
    own = at.BASE / "token"
    if not own.exists():
        own.write_text(secrets.token_urlsafe(8))
        own.chmod(0o600)
    return own.read_text().strip()


TOKEN = load_token()
# 合言葉を求めるか。既定は求めない（家の LAN の中で、どの端末からでも ?t= なしで開けるように。
# できるのは動画の操作だけ）。求めたいときは AUTOTUBE_REQUIRE_TOKEN=1。?t= を付けて開いても使える
REQUIRE_TOKEN = os.environ.get("AUTOTUBE_REQUIRE_TOKEN") == "1"


class Mpv:
    """常駐させた mpv を JSON IPC で操作する"""

    OBSERVE = ["pause", "time-pos", "duration", "fullscreen", "mute", "volume"]

    def __init__(self, on_event):
        self.on_event = on_event
        self.proc = None
        self.sock = None
        self.send_lock = threading.Lock()
        self.req_id = 0
        self.pending = {}  # request_id -> threading.Event（ping の応答待ち）

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and self.sock is not None

    def kill(self):
        """固まっていても確実に止める（SIGKILL）。意図した終了なので mpv-exit は通知しない"""
        proc, sock = self.proc, self.sock
        self.proc = self.sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait()

    def ping(self, timeout):
        """mpv が応答するか確かめる"""
        if not self.alive():
            return False
        with self.send_lock:
            self.req_id += 1
            rid = self.req_id
        ev = self.pending[rid] = threading.Event()
        self.command_raw({"command": ["get_property", "pid"], "request_id": rid})
        ok = ev.wait(timeout)
        self.pending.pop(rid, None)
        return ok

    @staticmethod
    def screen_args(maximized):
        if at.IS_WSL:
            return at.mpv_screen_args() if maximized else [*at.WSL_MPV_ARGS, "--geometry=60%x60%+80+80"]
        return ["--fs"] if maximized else []

    @staticmethod
    def kill_strays():
        """前の web.py が残した mpv（同じソケットを使うもの）を止める。2 重に映るのを防ぐ"""
        mark = f"--input-ipc-server={SOCK}".encode()
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                if mark in (d / "cmdline").read_bytes().split(b"\0"):
                    os.kill(int(d.name), signal.SIGKILL)
                    log(f"残っていた mpv（pid {d.name}）を止めました")
            except (OSError, ValueError):
                pass

    def ensure(self, maximized=True):
        if self.alive():
            return
        self.kill()
        self.kill_strays()
        SOCK.unlink(missing_ok=True)
        cmd = ["mpv", "--idle=yes", "--force-window=yes", *self.screen_args(maximized),
               f"--input-ipc-server={SOCK}", f"--input-conf={INPUT_CONF}",
               f"--ytdl-format={at.YTDL_FORMAT}", "--msg-level=all=no",
               "--title=${?media-title:${media-title}}${!media-title:autotube} - autotube"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=at.mpv_env())
        for _ in range(100):  # ソケットができるまで最大 10 秒待つ
            if SOCK.exists():
                try:
                    s = socket.socket(socket.AF_UNIX)
                    s.connect(str(SOCK))
                    self.sock = s
                    break
                except OSError:
                    pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        if self.sock is None:
            self.proc.kill()
            self.proc = None
            raise RuntimeError("mpv を起動できませんでした")
        threading.Thread(target=self._reader, args=(self.sock,), daemon=True).start()
        for i, name in enumerate(self.OBSERVE, 1):
            self.command("observe_property", i, name)

    def command(self, *args):
        self.command_raw({"command": list(args)})

    def command_raw(self, obj):
        sock = self.sock
        if sock is None:
            return
        data = (json.dumps(obj) + "\n").encode()
        with self.send_lock:
            try:
                sock.sendall(data)
            except OSError:
                pass

    def _reader(self, sock):
        buf = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "event" in obj:
                    self.on_event(obj)
                elif obj.get("request_id") in self.pending:
                    self.pending[obj["request_id"]].set()
        if self.sock is sock:  # kill() で止めたときは通知しない
            self.sock = None
            self.on_event({"event": "mpv-exit"})


# ---- ブラウザで再生するモード（/player）----
# mpv の代わりに、yt-dlp で落としたファイルを Brave の <video> で再生する。左に動画・右に次の曲の一覧のページ。
# Brave が GPU で再生できる VP9（webm）を優先、だめなら H.264（mp4）。テレビが 1080p なので 1080p まで（AV1 は GPU で再生できないので選ばない）
MEDIA_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "autotube" / "media"
WEB_FORMAT = ("bv*[height<=1080][ext=webm][vcodec^=vp]+ba[ext=webm]"
              "/bv*[height<=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4][height<=1080]/b")


def media_key(video):
    """ファイル名に使う名前。YouTube は動画の ID、それ以外（テレビに送る）は URL から作る"""
    vid = video.get("id") or ""
    if re.fullmatch(r"[\w-]{6,20}", vid) and not video.get("url"):
        return vid
    return "u" + hashlib.sha1((video.get("url") or vid).encode()).hexdigest()[:12]


class Media:
    """動画ファイルを先に落としておく係。状態は key -> loading / ready / error"""

    def __init__(self, on_change, keep=()):
        self.on_change = on_change
        self.lock = threading.Lock()
        self.state = {}
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        for f in MEDIA_DIR.iterdir():  # 前回の起動の残り（途中で止まったものも）は消す。keep（起動し直す前に流していた動画）は残す
            if f.name.split(".")[0] not in keep or ".part" in f.name:
                f.unlink(missing_ok=True)
        for k in keep:
            if self.path(k):
                self.state[k] = "ready"

    def path(self, key):
        if not re.fullmatch(r"[\w-]+", key):
            return None
        for f in MEDIA_DIR.glob(f"{key}.*"):
            if f.suffix in (".webm", ".mp4") and ".part" not in f.name and ".f" not in f.stem[len(key):]:
                return f
        return None

    def fetch(self, video):
        key = media_key(video)
        with self.lock:
            if self.state.get(key) in ("loading", "ready"):
                return
            self.state[key] = "loading"
        self.on_change()
        threading.Thread(target=self._run, args=(video, key), daemon=True).start()

    def _run(self, video, key):
        url = video.get("url") or f"https://www.youtube.com/watch?v={video['id']}"
        cmd = ["yt-dlp", "--extractor-args", "youtube:lang=ja", "--no-playlist", "-q", "--no-warnings",
               "-f", WEB_FORMAT, "-o", str(MEDIA_DIR / f"{key}.%(ext)s")]
        if video.get("cookies"):
            cmd += ["--cookies-from-browser", "brave"]
        t0 = time.time()
        r = subprocess.run(cmd + ["--", url], capture_output=True, text=True)
        ok = r.returncode == 0 and self.path(key) is not None
        log(f"ブラウザ用に落とした: {key} {'OK' if ok else 'NG'}（{int(time.time() - t0)} 秒）"
            + ("" if ok else f" {r.stderr.strip()[-300:]}"))
        with self.lock:
            self.state[key] = "ready" if ok else "error"
        self.on_change()

    def keep_only(self, keys):
        """keys 以外のファイルを消す（見終わった動画・キューから外れた動画）"""
        with self.lock:
            for k in list(self.state):
                if k not in keys and self.state[k] != "loading":
                    del self.state[k]
            loading = {k for k, v in self.state.items() if v == "loading"}
        for f in MEDIA_DIR.iterdir():
            k = f.name.split(".")[0]
            if k not in keys and k not in loading:
                f.unlink(missing_ok=True)


class Dubber:
    """英語の動画の吹き替えを裏で 1 本ずつ作っておく係（リモコンの「英語も（吹き替え）」のとき）。
    台本（時刻・配役・声・訳、dub.make_script）だけを作り、声はプレーヤーが流しながら作る（/tts）。
    状態は key -> checking（言語を確かめ中）/ working（訳している、progress に数）/ partial（頭から途中まで訳せた、流し始められる）
    / ready / none（日本語・字幕なし）/ error"""

    def __init__(self, on_change):
        self.on_change = on_change
        self.cv = threading.Condition()
        self.state, self.progress, self.todo = {}, {}, []
        self.covered = {}  # 途中までの台本が何秒まで訳せたか（シークバーに出す）
        self._last = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def want(self, video):
        vid = video.get("id") or ""
        if media_key(video) != vid:  # YouTube の動画だけ
            return
        with self.cv:
            if vid in self.state:
                return
            f = dubmod.find_script(vid, prefer=("",))
            if f and not json.loads(f.read_text()).get("partial"):
                self.state[vid] = "ready"
            else:
                self.state[vid] = "checking"
                self.todo.append(vid)
                self.cv.notify()
        self.on_change()

    def _set(self, vid, st, force=True):
        with self.cv:
            self.state[vid] = st
        if force or time.time() - self._last > 2:  # 進み具合は 2 秒に 1 回だけ知らせる
            self._last = time.time()
            self.on_change()

    def _loop(self):
        while True:
            with self.cv:
                while not self.todo:
                    self.cv.wait()
                vid = self.todo.pop(0)
            try:
                lang = dubmod.orig_lang(vid)  # 動画の情報か、元の言語の自動字幕から
                if lang is None or lang.split("-")[0] == "ja":  # 日本語（分からないものも）は吹き替えない
                    self._set(vid, "none")
                    continue
                log(f"吹き替えを作ります: {vid}（{lang}）")

                def prog(stage, k, n, vid=vid):
                    self.progress[vid] = f"訳しています {k}/{n}"
                    self._set(vid, self.state.get(vid) if self.state.get(vid) == "partial" else "working", force=False)
                self._set(vid, "working")
                # 元の言語（ar・he など）の字幕も探す。頭から訳せたところまでで partial にする（流し始められる）
                def partial(covered, vid=vid):
                    self.covered[vid] = covered
                    self._set(vid, "partial")
                dubmod.make_script(vid, orig=lang, progress=prog, on_partial=partial)
                self._set(vid, "ready")
                log(f"吹き替えができた: {vid}")
            except RuntimeError as e:  # 字幕が無いなど
                log(f"吹き替えなし: {vid} {e}")
                self._set(vid, "none")
            except Exception as e:
                log(f"吹き替えに失敗: {vid} {e!r}")
                self._set(vid, "error")


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.version = 0
        self.gen = 0               # 希望を変えるたびに増やす（古い選定結果を捨てる）
        self.request = ""
        self.opts = {"live": False, "long": False, "newest": False, "mode": "normal", "subs": False,
                     "music": False, "ytm": False, "en": False}
        self.traits = ""           # 「似たもの」「ネットの記事から」で選ぶときの基準
        self.exclude_channel = ""  # 「似たもの」のお手本（候補から外す）
        self.foreign = False       # 外国語の動画を観たい希望か（false なら日本語の動画だけを選ぶ）
        self.q720 = at.IS_WSL  # 画質を 720p までにする（WSL は CPU で描画するので既定でオン）
        self.queries, self.claude_live = [], False
        self.filters = {}          # 希望から読み取った絞り込み（並べ方・期間・再生数・長さ）
        self.kind = "topic"        # 希望の種類：music（音楽）/ channel（チャンネル）/ topic（それ以外）
        self.queue, self.now = [], None
        self.dropped = set()       # キューから外した動画（このセッション中は選ばない）
        self.channel = None        # {"id", "name"}：気に入ったチャンネルからだけ選ぶモード
        self.radio = None          # {"id", "title"}：その曲を起点にしたラジオ（似た曲）から選ぶモード
        self.picking = False
        self.play_on_fill = False  # キューが埋まったらすぐ再生する
        self.status = "希望を入力して「スタート」"
        self.player = {"pos": 0, "dur": 0, "paused": False, "maximized": True, "running": False,
                       "muted": False, "volume": 100}
        self.errors = 0
        self.progress_at = time.time()  # 最後に再生位置が進んだ時刻
        self.stalls = 0                 # 今の動画で読み込み直した回数
        self.mpv = Mpv(self.on_mpv_event)
        self.web = False           # ブラウザで再生するモード（/player のページを開くとオン）
        # ブラウザで再生中にリモコンから来た操作（一時停止など）。番号が増えたらページが実行する。
        # 番号は起動した時刻から始める（起動し直して 1 からにすると、開いたままのページが前の番号より小さいとみなして無視する）
        self.ctl = {"id": int(time.time())}
        self._restore()
        keep = {media_key(v) for v in ([self.now] if self.now else []) + self.queue[:1]}
        self.media = Media(self.changed, keep)
        self.dub = Dubber(self.changed)
        threading.Thread(target=self._dub_ahead, daemon=True).start()  # 起動し直す前のキューの分も
        # mpv の起動し直しを同時に 2 つ走らせない（走ると古い mpv が止められずに残り、画面に映り続ける）
        self.load_lock = threading.Lock()

    # ---- 起動し直しても希望・今の動画・キューを覚えておく（web.py を直して起動し直すたびに消えないように）----
    STATE_FILE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "autotube" / "web-state.json"
    SAVED = ("request", "opts", "queries", "claude_live", "kind", "filters", "traits", "channel", "radio",
             "now", "queue", "web")

    def _restore(self):
        try:
            d = json.loads(self.STATE_FILE.read_text())
            # 保存は変わったときだけなので、一晩止めておくと 6 時間を超えて消えていた。3 日より前のものだけ使わない
            if time.time() - d.get("t", 0) > 3 * 86400:
                return
        except (OSError, ValueError):
            return
        for k in self.SAVED:
            if k in d:
                setattr(self, k, d[k])
        if self.now or self.queue:
            self.status = "起動し直す前の続きです"
        log(f"前の状態を読み込みました: {self.request!r} キュー {len(self.queue)} 本")

    def _save(self):
        try:
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"t": time.time(), **{k: getattr(self, k) for k in self.SAVED}}, ensure_ascii=False))
            tmp.replace(self.STATE_FILE)
        except (OSError, TypeError, ValueError):
            pass

    # ---- 状態の通知 ----
    def changed(self):
        with self.cond:
            self.version += 1
            self.cond.notify_all()
        self._save()

    def ticking(self, msg):
        """長く待つ処理の間、経過秒数を付けて状態を出し続ける。止めるときは返り値の関数を呼ぶ"""
        stop = threading.Event()
        t0 = time.time()

        def run():
            while not stop.wait(1):
                with self.lock:
                    if not self.picking:
                        return
                    self.status = f"{msg}（{int(time.time() - t0)} 秒）"
                self.changed()
        self.set_status(msg)
        threading.Thread(target=run, daemon=True).start()
        return stop.set

    def set_status(self, s):
        at.log(s)
        with self.lock:
            self.status = s
        self.changed()

    def snapshot(self):
        with self.lock:
            return {"request": self.request, "opts": self.opts, "q720": self.q720, "queries": self.queries,
                    "claude_live": self.claude_live, "kind": self.kind, "filters": at.describe_filters(self.filters), "traits": self.traits, "channel": self.channel, "radio": self.radio, "now": self.now, "queue": self.queue,
                    "picking": self.picking, "status": self.status, "player": self.player,
                    "web": self.web, "media": self._media_snapshot(), "dub": self._dub_snapshot(), "ctl": self.ctl,
                    "can_show": self.WIN_FOCUS.exists()}

    def _media_snapshot(self):
        """ブラウザで再生するモードの、今とキューの動画のファイルの状態（key -> 状態）"""
        if not self.web:
            return {}
        out = {}
        for v in ([self.now] if self.now else []) + self.queue:
            k = media_key(v)
            v["key"] = k
            out[k] = self.media.state.get(k, "")
        return out

    def _dub_snapshot(self):
        """今とキューの動画の吹き替えの状態（key -> {st, msg}）"""
        out = {}
        for v in ([self.now] if self.now else []) + self.queue:
            k = v.get("id") or ""
            st = self.dub.state.get(k)
            if st:
                out[k] = {"st": st, "msg": self.dub.progress.get(k, "") if st in ("working", "partial") else "",
                          "covered": self.dub.covered.get(k) if st == "partial" else None}
        return out

    def _dub_ahead(self):
        """「英語も（吹き替え）」のとき、今とキューの動画の吹き替えを裏で作っておく（1 本 10 分ほどかかるので早めに）"""
        with self.lock:
            if not self.opts.get("en"):
                return
            vids = ([self.now] if self.now else []) + self.queue
        for v in vids:
            self.dub.want(v)

    # ---- 選定 ----
    def start(self, request, opts):
        request = request.strip()
        if not request:
            return
        with self.lock:
            self.gen += 1
            self.request = request
            self.opts = {k: bool(opts.get(k)) for k in ("live", "long", "newest", "subs", "music", "ytm", "en")}
            if self.opts["ytm"]:
                self.opts["music"] = True  # YouTube Music で選ぶなら音楽だけ
            self.opts["mode"] = opts.get("mode") if opts.get("mode") in ("normal", "similar", "web") else "normal"
            self.traits, self.exclude_channel = "", ""
            self.foreign = False
            self.queries, self.claude_live = [], False
            self.kind = "topic"
            self.filters = {}
            self.channel = None
            self.radio = None
            self.queue = []
            self.play_on_fill = True
            self.picking = False
            self.errors = 0
        self.changed()
        self.refill()

    def refill(self):
        with self.lock:
            if self.picking or not self.request or len(self.queue) >= QUEUE_LEN:
                return
            self.picking = True
            gen = self.gen
        self.changed()
        threading.Thread(target=self._pick, args=(gen,), daemon=True).start()

    def _pick(self, gen):
        try:
            with self.lock:
                request, opts, queries = self.request, dict(self.opts), list(self.queries)
                claude_live, channel, kind, filters = self.claude_live, self.channel, self.kind, dict(self.filters)
            if not queries:
                if opts["mode"] == "similar":
                    done = self.ticking("お手本のよいところを考えています…")
                    plan = at.plan_similar(request)
                elif opts["mode"] == "web":
                    done = self.ticking("ネットの記事から探しています（30 秒〜1 分）…")
                    plan = at.plan_web(request)
                else:
                    done = self.ticking("探し方を考えています…")
                    plan = at.plan_search(request, music=opts.get("music", False))
                done()
                queries, claude_live, kind, filters = plan["queries"], plan["live"], plan["kind"], plan["filters"]
                if opts.get("music") and opts["mode"] != "normal":
                    kind = "music"
                if opts.get("en") and kind == "topic" and not plan.get("foreign"):
                    # 「英語も」：日本語の希望だと検索ワードが日本語だけになり英語の動画が見つからないので、英語でも探す
                    done = self.ticking("英語の検索ワードも考えています…")
                    queries = queries + [q for q in at.english_queries(request, queries) if q not in queries]
                    done()
                if kind == "channel" and not channel:
                    # チャンネル名の希望：YouTube でチャンネルを探して「このチャンネル」モードにする
                    done = self.ticking(f"チャンネル「{plan['channel']}」を探しています…")
                    channel = at.find_channel(plan["channel"])
                    done()
                    if channel:
                        queries = [q for q in queries if q != request] or queries  # チャンネル内は短い言葉で
                    else:
                        kind = "topic"  # 見つからなければ普通に探す
                with self.lock:
                    if gen != self.gen:
                        return
                    self.queries, self.claude_live, self.kind, self.filters = queries, claude_live, kind, filters
                    self.traits = plan.get("traits", "")
                    self.exclude_channel = plan.get("exclude_channel", "")
                    self.foreign = plan.get("foreign", False)
                    if channel and not self.channel:
                        self.channel = channel
                self.changed()
            history = at.load_history()
            with self.lock:
                exclude = {h["id"] for h in history if h.get("event") == "played"}
                exclude |= self.dropped | {v["id"] for v in self.queue}
                if self.now:
                    exclude.add(self.now["id"])
                n = QUEUE_LEN - len(self.queue)
            with self.lock:
                radio = self.radio
                seed = (self.now or {}).get("id") if radio else None
            if radio:
                # ラジオ：起点の曲と、いま流れている曲（流れに合わせて少しずつ移っていく）のラジオから選ぶ
                seeds = [radio["id"]] + ([seed] if seed and seed != radio["id"] else [])
                cands = at.gather_radio(seeds, exclude=exclude, progress=lambda k, t, sid, c: self.set_status(
                    f"ラジオの曲を集めています {k}/{t}（候補 {c} 本）"))
                kind = "music"
            elif opts.get("ytm"):
                cands = at.gather_ytm(queries, exclude=exclude, progress=lambda k, t, q, c: self.set_status(
                    f"{q} {k}/{t}（候補 {c} 本）"))
                kind = "music"
            elif channel:
                cands = at.channel_videos(
                    channel["id"], channel["name"], queries, long=opts["long"], exclude=exclude,
                    progress=lambda k, t, q, c: self.set_status(
                        f"チャンネル「{channel['name']}」の動画を集めています {k}/{t}：{q}（候補 {c} 本）"))
                cands = [c for c in cands if at.passes(c, filters)]
            else:
                cands = at.gather(queries, normal=True, live=opts["live"] or claude_live,
                                  long=opts["long"], newest=opts["newest"], exclude=exclude, filters=filters,
                                  progress=lambda k, t, q, c: self.set_status(
                                      f"YouTube を検索中 {k}/{t}：「{q}」（候補 {c} 本）"))
            with self.lock:
                traits, ref, foreign = self.traits, self.exclude_channel, self.foreign
            if ref:  # 「似たもの」：お手本のチャンネル自身は外す
                key = ref.replace(" ", "").lower()
                cands = [c for c in cands if key not in (c.get("channel") or "").replace(" ", "").lower()]
            if gen != self.gen:
                return
            if not cands:
                self.set_status("観ていない候補が見つかりませんでした。希望を変えてみてください")
                return
            kw = dict(newest=opts["newest"], channel=channel and channel["name"], kind=kind, traits=traits,
                      radio=radio and radio["title"])
            # 日本語の動画だけにする（外国語の希望・音楽・チャンネル指定のときはしない）。選んだ後に言語を確かめる
            # en＝リモコンの「外国語も（吹き替え）」：外国語の動画も選び、吹き替えて流す
            japanese = not foreign and not opts.get("en") and kind != "music" and not channel
            if opts["subs"]:
                stopper = [lambda: None]

                def progress(stage, k, total):
                    stopper[0]()
                    if stage == "shortlist":
                        stopper[0] = self.ticking(f"【1/3】候補 {len(cands)} 本から字幕を読む分を絞っています…")
                    elif stage == "subs":
                        stopper[0] = lambda: None
                        self.set_status(f"【2/3】字幕を取っています… {k}/{total} 本")
                    else:
                        stopper[0] = self.ticking(f"【3/3】字幕が取れた {k}/{total} 本を読んで {n} 本選んでいます…")
                try:
                    if japanese:
                        picks = at.choose_japanese(request, cands, history, n=n, chooser=at.choose_with_subs,
                                                   progress=progress, **kw)
                    else:
                        picks = at.choose_with_subs(request, cands, history, n=n, progress=progress, **kw)
                finally:
                    stopper[0]()
            else:
                done = self.ticking(f"候補 {len(cands)} 本から Claude が {n} 本選んでいます…")
                try:
                    if japanese:
                        picks = at.choose_japanese(request, cands, history, n=n, **kw)
                    else:
                        picks = at.choose(request, cands, history, n=n, **kw)
                finally:
                    done()
            with self.lock:
                if gen != self.gen:
                    return
                have = {v["id"] for v in self.queue}
                for c, reason in picks:
                    if c["id"] not in have:
                        self.queue.append({"id": c["id"], "title": c.get("title") or "",
                                           "channel": c.get("channel"), "channel_id": c.get("channel_id"),
                                           "duration": c.get("duration"), "reason": reason})
                play_now = self.play_on_fill and self.queue
                if self.queue:
                    self.status = f"次の {len(self.queue)} 本を用意しました"
                elif japanese:
                    self.status = "希望に合う日本語の動画が見つかりませんでした（英語の動画は外しています）。希望を変えてみてください"
                else:
                    self.status = "希望に合う動画が見つかりませんでした。希望を変えてみてください"
        except Exception as e:  # 選定の失敗でサーバーを落とさない
            log(f"選定に失敗: {e!r}")
            self.set_status(f"選定に失敗しました: {e}")
            play_now = False
        finally:
            with self.lock:
                if gen == self.gen:
                    self.picking = False
            self.changed()
        self._dub_ahead()
        if play_now:
            self.next()

    # ---- 再生 ----
    def _load(self, video, start=0, restart=False):
        """mpv（必要なら起動し直して）に動画を読み込ませる"""
        with self.load_lock:
            self._load_locked(video, start, restart)

    def _load_locked(self, video, start, restart):
        with self.lock:
            maximized = self.player["maximized"]
            self.progress_at = time.time()  # 読み込み中は進まなくて当然
        if restart:
            self.mpv.kill()
        self.mpv.ensure(maximized)
        self.mpv.command("set_property", "force-media-title", video["title"])
        self.mpv.command("set_property", "ytdl-format", at.ytdl_format(720 if self.q720 else None))
        # 「テレビに送る」の動画はページの URL をそのまま。ログインが要るサイトは Brave の cookie を使う
        self.mpv.command("set_property", "ytdl-raw-options",
                         "cookies-from-browser=brave" if video.get("cookies") else "")
        url = video.get("url") or f"https://www.youtube.com/watch?v={video['id']}"
        if start > 5:
            self.mpv.command("loadfile", url, "replace", -1, f"start={int(start)}")
        else:
            self.mpv.command("loadfile", url, "replace")
        self.mpv.command("set_property", "pause", False)
        self.mpv.command("set_property", "mute", self.player["muted"])
        self.mpv.command("set_property", "volume", self.player["volume"])
        self.show_window()

    def show_window(self):
        """最小化や、ほかのウィンドウの裏に隠れていたら前に出す"""
        self.mpv.command("set_property", "window-minimized", False)
        self.mpv.command("set_property", "ontop", True)  # いったん最前面にしてすぐ戻す
        threading.Timer(1.0, self.mpv.command, ("set_property", "ontop", False)).start()

    def play(self, video):
        if self.web:
            return self._play_web(video)
        self.set_status(f"「{video['title'][:40]}」を読み込んでいます…")
        try:
            self._load(video, restart=RESTART_PER_VIDEO)
        except RuntimeError as e:
            self.set_status(str(e))
            return
        with self.lock:
            self.now = video
            self.play_on_fill = False
            self.player.update(pos=0, dur=video.get("duration") or 0, running=True)
            self.loading = True  # 再生位置が進み始めたら「再生中」にする
            self.status = f"「{video['title'][:40]}」を読み込んでいます…"
            self.progress_at, self.stalls = time.time(), 0
            request = self.request
        at.append_history({"event": "played", "ts": int(time.time()), "id": video["id"],
                           "title": video["title"], "channel": video.get("channel"),
                           "request": request, "reason": video.get("reason")})
        self.changed()
        self.refill()

    def _play_web(self, video):
        """ブラウザで再生するモード：ファイルを落とし始め（済んでいればすぐ）、ページに知らせる。次の 1 本も先に落とす"""
        with self.lock:
            self.now = video
            self.play_on_fill = False
            self.player.update(pos=0, dur=video.get("duration") or 0, running=True, paused=False)
            self.status = f"「{video['title'][:40]}」"
            request = self.request
        at.append_history({"event": "played", "ts": int(time.time()), "id": video["id"],
                           "title": video["title"], "channel": video.get("channel"),
                           "request": request, "reason": video.get("reason")})
        self.media.fetch(video)
        self._prefetch()
        self.changed()
        self.refill()

    def _prefetch(self):
        """キューの先頭を先に落としておき、今・先頭以外のファイルは消す"""
        with self.lock:
            keep = ([self.now] if self.now else []) + self.queue[:1]
        for v in keep[1:] if self.now else keep:
            self.media.fetch(v)
        self.media.keep_only({media_key(v) for v in keep})
        self._dub_ahead()

    def set_web(self, on):
        """ブラウザで再生するモードの切り替え。オンにすると mpv は止める。オフにすると今の動画を mpv で頭から"""
        with self.lock:
            if self.web == on:
                return
            self.web = on
            now = self.now
        if on:
            self.mpv.kill()
            if now:
                self._play_web(now)
            else:
                self._prefetch()
        else:
            self.media.keep_only(set())
            if now:
                self.play(now)
        self.set_status("ブラウザで再生します（/player）" if on else "mpv で再生します")

    def web_pos(self, b):
        """ページの <video> の再生位置（リモコンの表示用。2 秒ごとに来る）"""
        with self.lock:
            if not self.web:
                return
            self.player.update(pos=float(b.get("pos") or 0), dur=float(b.get("dur") or 0),
                               paused=bool(b.get("paused")), muted=bool(b.get("muted")),
                               volume=round(float(b.get("volume") or 0) * 100))
        self.changed()

    def _handle_stall(self, video, pos):
        """再生位置が進まないとき：読み込み直す。何度やってもだめなら次の動画へ"""
        with self.lock:
            if self.now is not video:
                return
            self.stalls += 1
            self.progress_at = time.time()
            give_up = self.stalls > STALL_RETRIES
        if give_up:
            log("再生が始まらないので次の動画へ進みます")
            self.set_status("再生が始まらなかったので次の動画へ進みました")
            self.next()
            return
        log(f"再生位置が {STALL_SEC} 秒進まないので読み込み直します（{self.stalls} 回目）")
        try:
            self._load(video, start=pos, restart=RESTART_PER_VIDEO)
            self.set_status(f"再生が止まっていたので読み込み直しました（{self.stalls} 回目）")
        except RuntimeError as e:
            self.set_status(str(e))

    def watchdog(self):
        """mpv が応答しなくなったら、強制終了して同じ動画を同じ位置から再開する"""
        while True:
            time.sleep(WATCH_INTERVAL)
            with self.lock:
                if self.web:  # ブラウザで再生中は mpv を見張らない
                    continue
                video, pos = self.now, self.player["pos"]
                stalled = (video is not None and not self.player["paused"]
                           and time.time() - self.progress_at > STALL_SEC)
            if stalled and self.mpv.alive():
                self._handle_stall(video, pos)
                continue
            if video is None or not self.mpv.alive() or self.mpv.ping(WATCH_TIMEOUT):
                continue
            with self.lock:
                if self.now is not video:  # 確認中に別の動画に切り替わった
                    continue
            log(f"mpv が応答しません。起動し直します（{int(pos)} 秒から）")
            try:
                self._load(video, start=pos, restart=True)
                self.set_status(f"mpv が応答しなくなったので起動し直しました（{at.fmt_dur(pos)} から再開）")
            except RuntimeError as e:
                self.set_status(str(e))

    def next(self):
        with self.lock:
            video = self.queue.pop(0) if self.queue else None
            if video is None:
                self.now = None
                self.play_on_fill = True
        if video:
            self.play(video)
        else:
            self.changed()
            self.refill()

    def play_index(self, i):
        with self.lock:
            if not 0 <= i < len(self.queue):
                return
            video = self.queue.pop(i)
        self.play(video)

    def drop_index(self, i):
        with self.lock:
            if not 0 <= i < len(self.queue):
                return
            self.dropped.add(self.queue.pop(i)["id"])
        if self.web:
            self._prefetch()
        self.changed()
        self.refill()

    def reshuffle(self):
        """キューを捨てて選び直す"""
        with self.lock:
            self.gen += 1
            self.dropped |= {v["id"] for v in self.queue}
            self.queue = []
            self.picking = False
        self.changed()
        self.refill()

    def set_channel(self, on):
        """今の動画のチャンネルからだけ次の候補を選ぶ（on=False で普通の選び方に戻す）"""
        with self.lock:
            now = self.now
            if on:
                if not now or not now.get("channel_id"):
                    self.status = "この動画はチャンネルが分からないので使えません"
                    on = None
                else:
                    self.channel = {"id": now["channel_id"], "name": now.get("channel") or "?"}
                    self.radio = None
            elif self.channel:
                self.channel = None
            else:
                on = None
            if on is not None:
                self.gen += 1          # 選定中の結果は捨てる
                self.queue = []
                self.picking = False
                self.status = (f"チャンネル「{self.channel['name']}」から選び直します" if on
                               else "普通の選び方に戻しました")
        self.changed()
        if on is not None:
            if on:
                self.mpv.command("show-text", f"📺 {self.channel['name']} から選びます")
                with self.lock:
                    video, request = self.now, self.request
                at.append_history({"event": "good", "ts": int(time.time()), "id": video["id"],
                                   "title": video["title"], "channel": video.get("channel"),
                                   "request": request})
            self.refill()

    def set_radio(self, on):
        """今の曲を起点にしたラジオ（似た曲）から次の候補を選ぶ（on=False で普通の選び方に戻す）"""
        with self.lock:
            now = self.now
            if on:
                if not now:
                    on = None
                else:
                    self.radio = {"id": now["id"], "title": now.get("title") or "?"}
                    self.channel = None
            elif self.radio:
                self.radio = None
            else:
                on = None
            if on is not None:
                self.gen += 1
                self.queue = []
                self.picking = False
                self.status = (f"「{self.radio['title']}」のラジオから選び直します" if on
                               else "普通の選び方に戻しました")
        self.changed()
        if on is not None:
            if on:
                self.mpv.command("show-text", "📻 この曲のラジオから選びます")
            self.refill()

    def quit_player(self):
        """mpv（再生アプリ）を終了してメモリを空ける。次に再生するときにまた起動する"""
        with self.load_lock, self.lock:
            self.now = None
            self.play_on_fill = False
            self.mpv.kill()
            self.player.update(running=False, maximized=True, pos=0, dur=0)
            self.status = "再生アプリを終了しました（キューの ▶ や「次へ」でまた起動します）"
        self.changed()

    def open_browser(self):
        """今の動画を、今の再生位置から Brave（YouTube のページ）で開く。コメントや概要欄を見る用。mpv は一時停止"""
        with self.lock:
            video, pos = self.now, int(self.player["pos"] or 0)
        if not video:
            self.set_status("再生中の動画がありません")
            return
        url = f"https://www.youtube.com/watch?v={video['id']}" + (f"&t={pos}s" if pos > 0 else "")
        if self.open_url(url):
            self.set_status("Brave で開きました（動画は一時停止中。F7 で戻れます）")

    def open_url(self, url, full=False):
        """URL を Brave で開いて前に出す（mpv は一時停止）。full なら Brave を全画面に（F8 で戻せる）。開けたら True"""
        self.mpv.command("set_property", "pause", True)
        if at.IS_WSL:
            cmd = ["cmd.exe", "/c", "start", "", url]
        else:
            self.mpv.command("set_property", "fullscreen", False)
            cmd = [shutil.which("brave-browser") or "brave-browser", url]
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, env=at.mpv_env())
        except OSError as e:
            self.set_status(f"ブラウザを開けませんでした：{e}")
            return False
        if not at.IS_WSL and shutil.which("wlrctl"):
            time.sleep(1.5)  # タブが開いてから Brave を前に出す
            subprocess.run(["wlrctl", "toplevel", "focus", "app_id:brave-browser"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=at.mpv_env())
            if full:
                subprocess.run(["wlrctl", "toplevel", "fullscreen", "app_id:brave-browser", "state:active"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=at.mpv_env())
                threading.Thread(target=self._video_fullscreen, daemon=True).start()
        return True

    UKEY = Path.home() / ".local/bin/ukey"  # 本物と同じキー番号でキーを押す（tonzura だけにある）
    WIN_FOCUS = Path.home() / ".local/bin/win-focus"  # 窓・タブを前に出す（tonzura だけにある。F7＋M と同じ）

    BOOKMARKS = Path.home() / ".config/BraveSoftware/Brave-Browser/Default/Bookmarks"  # Brave があるのは tonzura だけ
    FAVICONS = BOOKMARKS.with_name("Favicons")
    _bm_cache = (None, [])

    def bookmarks(self):
        """Brave のブックマークバーの中身（フォルダーの中は「フォルダー名 / 名前」で平らに）。アイコンは Brave の Favicons から"""
        try:
            mtime = self.BOOKMARKS.stat().st_mtime
        except OSError:
            return []
        if self._bm_cache[0] == mtime:
            return self._bm_cache[1]
        try:
            root = json.loads(self.BOOKMARKS.read_text())["roots"]["bookmark_bar"]
        except (OSError, ValueError, KeyError):
            return []
        items = []

        def walk(node, prefix=""):
            for c in node.get("children", []):
                if c.get("type") == "url" and c.get("url", "").startswith(("http://", "https://")):
                    items.append({"name": prefix + c.get("name", ""), "url": c["url"]})
                elif c.get("type") == "folder":
                    walk(c, prefix + c.get("name", "") + " / ")
        walk(root)
        icons = self._favicons([i["url"] for i in items])
        for i in items:
            i["icon"] = icons.get(i["url"], "")
        self._bm_cache = (mtime, items)
        return items

    def _favicons(self, urls):
        """URL -> data: の URI。そのページのアイコンが無ければ同じホストのページのもの（Brave が使っている DB は写してから読む）"""
        import base64
        import sqlite3
        import tempfile
        out = {}
        try:
            with tempfile.TemporaryDirectory() as d:
                db = Path(d) / "fav.db"
                shutil.copy(self.FAVICONS, db)
                con = sqlite3.connect(db)
                for u in urls:
                    host = urlparse(u).netloc
                    q = ("SELECT b.image_data FROM icon_mapping m JOIN favicon_bitmaps b ON b.icon_id = m.icon_id "
                         "WHERE {} ORDER BY abs(b.width - 32) LIMIT 1")
                    row = con.execute(q.format("m.page_url = ?"), (u,)).fetchone() or \
                        con.execute(q.format("(m.page_url LIKE ? OR m.page_url LIKE ?)"),
                                    (f"https://{host}/%", f"http://{host}/%")).fetchone()
                    if row and row[0]:
                        out[u] = "data:image/png;base64," + base64.b64encode(row[0]).decode()
                con.close()
        except (OSError, sqlite3.Error):
            pass
        return out

    def open_hosts(self):
        """テレビの Brave で開いているタブのホストの一覧（tvfs hosts）。ブックマークの「もうタブがある」の印に"""
        tvfs = Path.home() / ".local/bin/tvfs"
        if not tvfs.exists():
            return []
        try:
            r = subprocess.run([str(tvfs), "hosts"], capture_output=True, text=True, timeout=3, env=at.mpv_env())
            return json.loads(r.stdout) if r.returncode == 0 else []
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return []

    def open_bookmark(self, url, how, solo=False):
        """テレビの Brave で URL を開く（拡張機能 tv-fullscreen に中継 tvfs で）。how: site / new / here"""
        if not url.startswith(("http://", "https://")) or how not in ("site", "new", "here"):
            return
        env = at.mpv_env()
        tvfs = Path.home() / ".local/bin/tvfs"
        if subprocess.run(["wlrctl", "toplevel", "find", "app_id:brave-browser"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            subprocess.Popen(["brave-browser", url], env=env, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        subprocess.run(["wlrctl", "toplevel", "focus", "app_id:brave-browser"], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not tvfs.exists() or subprocess.run([str(tvfs), "open", url, how], env=env,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            subprocess.Popen(["brave-browser", url], env=env, start_new_session=True,  # 中継が使えなければ新しいタブで
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        solo_here = self.WIN_FOCUS.with_name("solo-here")
        if solo and solo_here.exists():  # F7 と同じく、行き先以外の動画を一時停止・行き先で止めていたものは再開
            subprocess.run([str(solo_here)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def jump(self, where, solo=False):
        """テレビの画面を 端末（ttyd のタブ）か 動画（mpv かプレーヤーのタブ）へ（F7＋T／F7＋M と同じ）。
        solo なら F7 と同じく、行き先以外の動画を一時停止・行き先で止めていたものは再開"""
        arg = {"term": "ttyd", "video": "mpv"}.get(where)
        if not arg or not self.WIN_FOCUS.exists():
            return
        env = at.mpv_env()
        subprocess.run([str(self.WIN_FOCUS), arg], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        solo_here = self.WIN_FOCUS.with_name("solo-here")
        if solo and solo_here.exists():
            subprocess.run([str(solo_here)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    _solo_timer = None
    _tabs_lock = threading.Lock()  # 続けて押されても 1 つずつ順に（前の移動が終わる前に同じタブを基準に動かないように）

    VIDEO_SEEK = {"back180": -180, "back60": -60, "back": -10, "fwd": 10, "fwd60": 60, "fwd180": 180}

    def video(self, cmd):
        """テレビの手前の動画を操作する（F6＋キーと同じ。リモコンのブックマークのページの動画のボタン・シークバー）。
        手前が mpv なら IPC で、それ以外（Brave）なら拡張機能 tv-fullscreen に中継 tvfs で。cmd: back180〜fwd180・toggle・volup・voldown・pct:NN"""
        if not (cmd in self.VIDEO_SEEK or cmd in ("toggle", "volup", "voldown") or re.fullmatch(r"pct:\d+(\.\d+)?", cmd)):
            return
        env = at.mpv_env()
        front = subprocess.run(["wlrctl", "toplevel", "list", "state:active"], capture_output=True, text=True,
                               env=env).stdout.partition(":")[0].strip()
        if front == "mpv" and self.mpv.alive():
            if cmd in self.VIDEO_SEEK:
                self.mpv.command("seek", self.VIDEO_SEEK[cmd], "relative")
                self.mpv.command("show-progress")
            elif cmd == "toggle":
                self.mpv.command("cycle", "pause")
            elif cmd in ("volup", "voldown"):
                with self.lock:
                    v = max(0, min(100, int(self.player["volume"]) + (10 if cmd == "volup" else -10)))
                    self.player["volume"] = v
                self.mpv.command("set_property", "volume", v)
                self.mpv.command("show-text", f"音量 {v}")
                self.changed()
            else:
                self.mpv.command("seek", float(cmd[4:]), "absolute-percent")
                self.mpv.command("show-progress")
            return
        tvfs = Path.home() / ".local/bin/tvfs"
        if tvfs.exists():
            subprocess.run([str(tvfs), "video", cmd], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def fullscreen(self):
        """F6（単独）と同じく、テレビの手前の窓に F9 を送る（Brave なら拡張機能 tv-fullscreen が動画を全画面に、もう一度で戻る）。
        ページからの命令では全画面にできない（ユーザーの操作が要る）ので、本物のキーとして pkey で送る"""
        pkey = Path.home() / ".local/bin/pkey"
        if pkey.exists():
            subprocess.run([str(pkey), "f9"], env=at.mpv_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tabs(self, how, solo=False):
        """テレビの Brave のタブを左・右へ、手前のタブを閉じる（拡張機能 tv-fullscreen に中継 tvfs で）。
        solo なら、最後に押してから 1 秒たったところで solo-here（続けて押して通り過ぎたタブの動画を鳴らさない）"""
        tvfs = Path.home() / ".local/bin/tvfs"
        if how not in ("prev", "next", "close") or not tvfs.exists():
            return
        env = at.mpv_env()
        if subprocess.run(["wlrctl", "toplevel", "find", "app_id:brave-browser"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return
        with self._tabs_lock:
            subprocess.run(["wlrctl", "toplevel", "focus", "app_id:brave-browser"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([str(tvfs), "tabs", how], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        solo_here = self.WIN_FOCUS.with_name("solo-here")
        with self.lock:
            if self._solo_timer:
                self._solo_timer.cancel()
            self._solo_timer = None
            if solo and solo_here.exists():
                self._solo_timer = threading.Timer(1.0, subprocess.run, args=([str(solo_here)],),
                                                   kwargs={"env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})
                self._solo_timer.daemon = True
                self._solo_timer.start()

    def show_video(self):
        """テレビの画面を動画に切り替える：mpv があれば mpv の窓、無ければ Brave のプレーヤーのタブ（無ければ開く）。
        そのあと solo-here（Shift＋F7 と同じ）で、ほかのタブの動画を一時停止し、ここで止めていたものは再開する"""
        if not self.WIN_FOCUS.exists():
            return
        for cmd in (self.WIN_FOCUS, self.WIN_FOCUS.with_name("solo-here")):
            if cmd.exists():
                subprocess.run([str(cmd), *(["mpv"] if cmd == self.WIN_FOCUS else [])],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=at.mpv_env())
    FULL_WAIT = 6  # ページが読み込まれるのを待つ秒数

    def _video_fullscreen(self):
        """ページが読み込まれたころに F9 を押して、Brave の拡張機能「テレビで全画面」に動画を全画面にさせる"""
        if not self.UKEY.exists():
            return
        time.sleep(self.FULL_WAIT)
        env = at.mpv_env()
        if subprocess.run(["wlrctl", "toplevel", "find", "app_id:brave-browser", "state:active"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env).returncode != 0:
            return  # その間にほかの窓に移っていたら押さない
        subprocess.run([str(self.UKEY), "f9"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    # ---- テレビに送る（別の PC・スマホで見ていた動画を、この PC で続きから再生） ----
    YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/)|youtu\.be/)([\w-]{11})")

    def cast(self, url, start=0):
        """mpv（yt-dlp）で再生できればそうし、できなければ Brave でページを開く"""
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            self.set_status("送られた URL が正しくありません")
            return
        start = max(0, int(float(start or 0)))
        self.set_status("送られた動画を調べています…")
        m = self.YT_ID.search(url)
        if m:  # YouTube はいつもどおり（タイトルだけ取る）
            info = self._probe(f"https://www.youtube.com/watch?v={m.group(1)}", False)
            video = {"id": m.group(1), "title": (info or {}).get("title") or "送られた動画",
                     "duration": (info or {}).get("duration") or 0, "channel": (info or {}).get("channel")}
        else:
            cookies = False
            info = self._probe(url, False)
            if not info:
                cookies, info = True, self._probe(url, True)  # ログインが要るサイトかもしれない
            if not info:
                self.set_status("mpv では再生できないサイトなので、Brave で開きます")
                if self.open_url(url, full=True):
                    self.set_status("mpv では再生できないので Brave で開きました（再生ボタンは画面で押してください。全画面は F8 で戻せます）")
                return
            video = {"id": f"cast:{info.get('extractor_key', '')}:{info.get('id', '')}", "url": url,
                     "title": info.get("title") or url, "duration": info.get("duration") or 0,
                     "channel": info.get("uploader") or info.get("extractor_key"), "cookies": cookies}
        video["cast"] = True
        video["reason"] = "ほかの端末から送られた動画"
        try:
            self._load(video, start=start)
        except RuntimeError as e:
            self.set_status(str(e))
            return
        with self.lock:
            self.now = video
            self.play_on_fill = False
            self.player.update(pos=start, dur=video["duration"], running=True)
            self.loading = True
            self.status = f"送られた「{video['title'][:40]}」を読み込んでいます…"
            self.progress_at, self.stalls = time.time(), 0
        self.changed()

    @staticmethod
    def _probe(url, cookies):
        """yt-dlp で再生できるか調べる。できればタイトルなどを返す"""
        cmd = ["yt-dlp", "-J", "--no-playlist", "--skip-download", "--no-warnings", "-q",
               "--extractor-args", "youtube:lang=ja"]
        if cookies:
            cmd += ["--cookies-from-browser", "brave"]
        try:
            r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=40)
            info = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return None
        if not info or not (info.get("formats") or info.get("url")):
            return None
        return info

    def stop(self):
        with self.lock:
            self.now = None
            self.play_on_fill = False
            self.status = "停止しました"
        self.mpv.command("stop")
        self.changed()

    def rate(self, ev):
        with self.lock:
            video, request = self.now, self.request
        if not video or ev not in ("good", "bad"):
            return
        at.append_history({"event": ev, "ts": int(time.time()), "id": video["id"],
                           "title": video["title"], "channel": video.get("channel"),
                           "request": request})
        self.mpv.command("show-text", "👍 記録しました" if ev == "good" else "👎 記録しました")
        self.set_status("👍 を記録しました" if ev == "good" else "👎 を記録しました")

    def set_q720(self, on):
        """画質の上限を切り替える。再生中の動画は今の位置から読み込み直す"""
        with self.lock:
            if self.q720 == bool(on):
                return
            self.q720 = bool(on)
            video, pos = self.now, self.player["pos"]
        self.changed()
        if video and self.mpv.alive():
            try:
                self._load(video, start=pos, restart=RESTART_PER_VIDEO)
            except RuntimeError as e:
                self.set_status(str(e))

    def web_ctl(self, cmd, arg=None):
        """ブラウザで再生中：操作をプレーヤーのページに送る（状態の ctl に番号つきで載せ、ページが実行する）"""
        with self.lock:
            self.ctl = {"id": self.ctl["id"] + 1, "cmd": cmd, "arg": arg}
        self.changed()

    def toggle_pause(self):
        if self.web:
            return self.web_ctl("pause")
        self.mpv.command("cycle", "pause")

    def set_volume(self, vol=None, delta=None):
        with self.lock:
            v = self.player["volume"] + delta if delta is not None else vol
            v = max(0, min(100, int(v)))
            self.player["volume"] = v
        if self.web:
            return self.web_ctl("volume", v)
        self.mpv.command("set_property", "volume", v)
        self.changed()

    def toggle_mute(self):
        if self.web:
            return self.web_ctl("mute")
        with self.lock:
            muted = not self.player["muted"]
        self.mpv.command("set_property", "mute", muted)

    def seek(self, sec):
        if self.web:
            return self.web_ctl("seek", sec)
        self.mpv.command("seek", sec, "relative")

    def toggle_max(self):
        if not self.mpv.alive():
            return
        self.show_window()
        if at.IS_WSL:  # WSLg では fullscreen が使えないので、枠なしの画面サイズ ⇔ 普通のウィンドウ
            with self.lock:
                maximized = not self.player["maximized"]
                self.player["maximized"] = maximized
            if maximized:  # 実行中の +0+0 指定は効かないため +1+1
                self.mpv.command("set_property", "geometry", "100%x100%+1+1")
                self.mpv.command("set_property", "border", False)
            else:
                self.mpv.command("set_property", "border", True)
                self.mpv.command("set_property", "geometry", "60%x60%+80+80")
            self.changed()
        else:
            self.mpv.command("cycle", "fullscreen")

    # ---- mpv からのイベント ----
    def on_mpv_event(self, ev):
        name = ev.get("event")
        if self.web:  # ブラウザで再生中は、止めた mpv からの知らせを無視する
            return
        if name == "property-change":
            key, val = ev.get("name"), ev.get("data")
            with self.lock:
                p = self.player
                if key == "time-pos" and val is not None:
                    if int(val) == int(p["pos"]):
                        return
                    p["pos"] = val
                    self.progress_at = time.time()
                    if getattr(self, "loading", False) and val > 0.5:
                        self.loading = False
                        if not self.picking:
                            self.status = "再生中"
                elif key == "duration" and val:
                    p["dur"] = val
                elif key == "pause":
                    p["paused"] = bool(val)
                    self.progress_at = time.time()  # 一時停止の前後は止まっていて当然
                elif key == "mute":
                    p["muted"] = bool(val)
                elif key == "volume" and val is not None:
                    p["volume"] = round(val)
                elif key == "fullscreen" and not at.IS_WSL:
                    p["maximized"] = bool(val)
                else:
                    return
            self.changed()
        elif name == "file-loaded":
            self.errors = 0
        elif name == "end-file":
            reason = ev.get("reason")
            if reason == "eof":
                self.next()
            elif reason == "error" and (self.now or {}).get("cast") and (self.now or {}).get("url"):
                url = self.now["url"]
                with self.lock:  # mpv では何も再生していないので「再生中」を空にする
                    self.now = None
                    self.player.update(pos=0, dur=0)
                self.set_status("mpv で再生できなかったので Brave で開きました（再生ボタンは画面で押してください）")
                threading.Thread(target=self.open_url, args=(url, True), daemon=True).start()
            elif reason == "error":
                self.errors += 1
                if self.errors >= MAX_ERRORS:
                    self.set_status("再生エラーが続いたので止めました")
                    with self.lock:
                        self.now = None
                    self.changed()
                else:
                    self.set_status("再生できなかったので次へ進みます")
                    threading.Timer(2, self.next).start()
        elif name == "client-message":  # mpv-input-web.conf のキー
            args = ev.get("args") or []
            cmd = args[0] if args else ""
            if cmd == "autotube-next":
                self.next()
            elif cmd in ("autotube-good", "autotube-bad"):
                self.rate(cmd.split("-")[1])
        elif name == "mpv-exit":
            with self.lock:
                self.now = None
                self.play_on_fill = False
                self.player.update(running=False, maximized=True)
                self.status = "mpv が閉じられました。キューの動画を選ぶか「次へ」で再開します"
            self.changed()


app = App()

def wake_screen():
    """リモコンで操作したら、消えた画面（swayidle の wlopm --off）をつける。リモコンの操作は入力に数えられないため。
    wlopm が無い PC（Pi）では何もしない"""
    if not shutil.which("wlopm"):
        return
    env = at.mpv_env()
    try:
        if " off" in subprocess.run(["wlopm"], env=env, capture_output=True, text=True, timeout=2).stdout:
            subprocess.run(["wlopm", "--on", "*"], env=env, timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass


ACTIONS = {
    "start": lambda b: app.start(b.get("request", ""), b.get("opts") or {}),
    "next": lambda b: app.next(),
    "play": lambda b: app.play_index(int(b.get("index", -1))),
    "drop": lambda b: app.drop_index(int(b.get("index", -1))),
    "reshuffle": lambda b: app.reshuffle(),
    "stop": lambda b: app.stop(),
    "quit": lambda b: app.quit_player(),
    "browser": lambda b: app.open_browser(),
    "show": lambda b: app.show_video(),
    "fullscreen": lambda b: app.fullscreen(),
    "video": lambda b: app.video(str(b.get("cmd", ""))),
    "tabs": lambda b: app.tabs(str(b.get("how", "")), bool(b.get("solo"))),
    "jump": lambda b: app.jump(str(b.get("where", "")), bool(b.get("solo"))),
    "openurl": lambda b: app.open_bookmark(str(b.get("url", "")), str(b.get("how", "site")), bool(b.get("solo"))),
    "pause": lambda b: app.toggle_pause(),
    "mute": lambda b: app.toggle_mute(),
    "volume": lambda b: app.set_volume(b.get("vol"), b.get("delta")),
    "seek": lambda b: app.seek(float(b.get("sec", 0))),
    "max": lambda b: app.toggle_max(),
    "rate": lambda b: app.rate(b.get("ev")),
    "channel": lambda b: app.set_channel(bool(b.get("on"))),
    "radio": lambda b: app.set_radio(bool(b.get("on"))),
    "q720": lambda b: app.set_q720(b.get("on")),
    "cast": lambda b: app.cast(b.get("url", ""), b.get("t", 0)),
    "web": lambda b: app.set_web(bool(b.get("on"))),
    "webpos": lambda b: app.web_pos(b),
}

CAST_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>テレビに送りました</title><body style="font:16px sans-serif;padding:16px">
<p>📺 テレビに送りました。</p><p><small>この窓は閉じて大丈夫です</small></p>
<script>setTimeout(() => window.close(), 1500)</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if not REQUIRE_TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        return secrets.compare_digest(q.get("t", [""])[0], TOKEN) or \
            secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authed():
            return self._send(403, "合言葉（?t=）が違います", "text/plain; charset=utf-8")
        if path in ("/", "/index.html"):
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/bookmarks":
            self._send(200, (WEB_DIR / "bookmarks.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/player":
            self._send(200, (WEB_DIR / "player.html").read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/media/"):
            self._media(path.removeprefix("/media/"))
        # ---- 吹き替えの実験（台本だけを持ち、流しながら声にする）：/dubtest?id=動画 ----
        elif path == "/dubtest":
            self._send(200, (WEB_DIR / "dubtest.html").read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/script/"):  # 台本（~/.cache/autotube/dub/ID.script.json）
            key = path.removeprefix("/script/")
            var = parse_qs(urlparse(self.path).query).get("v", [""])[0]  # 比べる版（opus など）
            f = dubmod.script_path(key, var if re.fullmatch(r"\w+", var or "-") else "")
            if re.fullmatch(r"[\w-]+", key) and f.exists():
                self._send(200, f.read_bytes())
            else:
                self._send(404, '{"error":"no script"}')
        elif path.startswith("/dubvideo/"):  # 実験用に落とした動画（~/.cache/autotube/dub/ID.video.*）
            def find(key):
                if not re.fullmatch(r"[\w-]+", key):
                    return None
                return next((f for f in dubmod.OUT.glob(f"{key}.video.*") if ".part" not in f.name), None)
            self._media(path.removeprefix("/dubvideo/"), find)
        elif path == "/tts":  # 文を声に（VOICEVOX）：/tts?text=&sp=話者&speed=速さ
            q = parse_qs(urlparse(self.path).query)
            try:
                pcm = dubmod.tts(q.get("text", [""])[0][:400], int(q.get("sp", ["3"])[0]),
                                 max(0.5, min(2.5, float(q.get("speed", ["1.15"])[0]))),
                                 max(-0.15, min(0.15, float(q.get("pitch", ["0"])[0]))))
                import io, wave
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(dubmod.RATE); w.writeframes(pcm.tobytes())
                self._send(200, buf.getvalue(), "audio/wav")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/openhosts":
            self._send(200, json.dumps(app.open_hosts()))
        elif path == "/api/bookmarks":
            self._send(200, json.dumps(app.bookmarks(), ensure_ascii=False))
        elif path == "/api/state":
            self._send(200, json.dumps(app.snapshot(), ensure_ascii=False))
        elif path == "/events":
            self._events()
        elif path == "/cast":  # ブックマークレットから開く：/cast?url=…&t=秒
            q = parse_qs(urlparse(self.path).query)
            threading.Thread(target=app.cast, args=(q.get("url", [""])[0], q.get("t", ["0"])[0]),
                             daemon=True).start()
            self._send(200, CAST_PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self._authed():
            return self._send(403, '{"error":"forbidden"}')
        name = urlparse(self.path).path.removeprefix("/api/")
        if name not in ACTIONS:
            return self._send(404, '{"error":"not found"}')
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            # 重い処理（mpv 起動など）で応答を待たせないよう別スレッドで実行する
            threading.Thread(target=ACTIONS[name], args=(body,), daemon=True).start()
            threading.Thread(target=wake_screen, daemon=True).start()
        except (ValueError, TypeError) as e:
            return self._send(400, json.dumps({"error": str(e)}))
        self._send(200, '{"ok":true}')

    def _media(self, key, find=None):
        """ブラウザで再生する動画ファイル（と吹き替えの音）を配る。シークのために部分の取得（Range）に対応"""
        f = (find or app.media.path)(key)
        if not f:
            return self._send(404, '{"error":"not found"}')
        size = f.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").split(",")[0].strip())
        if m and (m[1] or m[2]):
            if m[1]:
                start, end = int(m[1]), int(m[2]) if m[2] else size - 1
            else:
                start = max(0, size - int(m[2]))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "video/webm")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with f.open("rb") as fp:
                fp.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fp.read(min(1 << 18, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _events(self):
        """Server-Sent Events で状態が変わるたびに送る"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = -1
        try:
            while True:
                with app.cond:
                    app.cond.wait_for(lambda: app.version != last, timeout=15)
                    changed, last = app.version != last, app.version
                if changed:
                    msg = "data: " + json.dumps(app.snapshot(), ensure_ascii=False) + "\n\n"
                else:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main():
    at.ensure_prefs()
    Mpv.kill_strays()
    # kill（SIGTERM）で止められたときも mpv を道連れにする（残ると次の起動で 2 重になる）
    def on_term(*_):
        # 字幕の取得などの途中でも待たずに終わる（待つと次の web.py がポートを使えず起動できない）
        app.mpv.kill()
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    threading.Thread(target=app.watchdog, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    # HTTPS の入口（phone-input を HTTPS で開いたとき、その中に出すため。HTTPS のページの中に http のページは出せない）。
    # 証明書は mkcert で作った自分用の認証局のもの（tonzura の ~/.local/share/tonzura-tls）。無ければ開かない
    tls = Path.home() / ".local/share/tonzura-tls"
    if (tls / "cert.pem").exists() and (tls / "key.pem").exists():
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls / "cert.pem", tls / "key.pem")
        https = ThreadingHTTPServer((HOST, int(os.environ.get("AUTOTUBE_HTTPS_PORT", "8776"))), Handler)
        https.daemon_threads = True
        https.socket = ctx.wrap_socket(https.socket, server_side=True)
        threading.Thread(target=https.serve_forever, daemon=True).start()
    log(f"http://localhost:{PORT}/" + (f"?t={TOKEN}" if REQUIRE_TOKEN else "") + " で待っています（Ctrl+C で終了）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.mpv.kill()


if __name__ == "__main__":
    main()
