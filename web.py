#!/usr/bin/env python3
"""autotube の Web リモコン。再生は mpv（テレビ側）、操作はブラウザ（スマホ・PC）から。

使い方:
  python3 web.py            # http://<このマシン>:8766/ を開く
  AUTOTUBE_PORT=9000 python3 web.py

mpv は 1 つを常駐させ、JSON IPC（--input-ipc-server）で動画の切り替え・一時停止・最大化などを行う。
次に流す動画は Claude に 5 本まとめて選んでもらい、キューに積んでおく。
mpv が応答しなくなったら（見張り役が数秒ごとに確認）、強制終了して同じ動画を同じ位置から再開する。
"""
import json
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


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.version = 0
        self.gen = 0               # 希望を変えるたびに増やす（古い選定結果を捨てる）
        self.request = ""
        self.opts = {"live": False, "long": False, "newest": False, "mode": "normal", "subs": False}
        self.traits = ""           # 「似たもの」「ネットの記事から」で選ぶときの基準
        self.exclude_channel = ""  # 「似たもの」のお手本（候補から外す）
        self.q720 = at.IS_WSL  # 画質を 720p までにする（WSL は CPU で描画するので既定でオン）
        self.queries, self.claude_live = [], False
        self.filters = {}          # 希望から読み取った絞り込み（並べ方・期間・再生数・長さ）
        self.kind = "topic"        # 希望の種類：music（音楽）/ channel（チャンネル）/ topic（それ以外）
        self.queue, self.now = [], None
        self.dropped = set()       # キューから外した動画（このセッション中は選ばない）
        self.channel = None        # {"id", "name"}：気に入ったチャンネルからだけ選ぶモード
        self.picking = False
        self.play_on_fill = False  # キューが埋まったらすぐ再生する
        self.status = "希望を入力して「スタート」"
        self.player = {"pos": 0, "dur": 0, "paused": False, "maximized": True, "running": False,
                       "muted": False, "volume": 100}
        self.errors = 0
        self.progress_at = time.time()  # 最後に再生位置が進んだ時刻
        self.stalls = 0                 # 今の動画で読み込み直した回数
        self.mpv = Mpv(self.on_mpv_event)
        # mpv の起動し直しを同時に 2 つ走らせない（走ると古い mpv が止められずに残り、画面に映り続ける）
        self.load_lock = threading.Lock()

    # ---- 状態の通知 ----
    def changed(self):
        with self.cond:
            self.version += 1
            self.cond.notify_all()

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
                    "claude_live": self.claude_live, "kind": self.kind, "filters": at.describe_filters(self.filters), "traits": self.traits, "channel": self.channel, "now": self.now, "queue": self.queue,
                    "picking": self.picking, "status": self.status, "player": self.player}

    # ---- 選定 ----
    def start(self, request, opts):
        request = request.strip()
        if not request:
            return
        with self.lock:
            self.gen += 1
            self.request = request
            self.opts = {k: bool(opts.get(k)) for k in ("live", "long", "newest", "subs")}
            self.opts["mode"] = opts.get("mode") if opts.get("mode") in ("normal", "similar", "web") else "normal"
            self.traits, self.exclude_channel = "", ""
            self.queries, self.claude_live = [], False
            self.kind = "topic"
            self.filters = {}
            self.channel = None
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
                    plan = at.plan_search(request)
                done()
                queries, claude_live, kind, filters = plan["queries"], plan["live"], plan["kind"], plan["filters"]
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
            if channel:
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
                traits, ref = self.traits, self.exclude_channel
            if ref:  # 「似たもの」：お手本のチャンネル自身は外す
                key = ref.replace(" ", "").lower()
                cands = [c for c in cands if key not in (c.get("channel") or "").replace(" ", "").lower()]
            if gen != self.gen:
                return
            if not cands:
                self.set_status("観ていない候補が見つかりませんでした。希望を変えてみてください")
                return
            kw = dict(newest=opts["newest"], channel=channel and channel["name"], kind=kind, traits=traits)
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
                    picks = at.choose_with_subs(request, cands, history, n=n, progress=progress, **kw)
                finally:
                    stopper[0]()
            else:
                done = self.ticking(f"候補 {len(cands)} 本から Claude が {n} 本選んでいます…")
                try:
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
                self.status = f"次の {len(self.queue)} 本を用意しました"
        except Exception as e:  # 選定の失敗でサーバーを落とさない
            log(f"選定に失敗: {e!r}")
            self.set_status(f"選定に失敗しました: {e}")
            play_now = False
        finally:
            with self.lock:
                if gen == self.gen:
                    self.picking = False
            self.changed()
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
        self.mpv.command("set_property", "ytdl-format", at.ytdl_format(720 if self.q720 else 1080))
        url = f"https://www.youtube.com/watch?v={video['id']}"
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
            return
        if not at.IS_WSL and shutil.which("wlrctl"):
            time.sleep(1.5)  # タブが開いてから Brave を前に出す
            subprocess.run(["wlrctl", "toplevel", "focus", "app_id:brave-browser"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=at.mpv_env())
        self.set_status("Brave で開きました（動画は一時停止中。F7 で戻れます）")

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

    def toggle_pause(self):
        self.mpv.command("cycle", "pause")

    def set_volume(self, vol=None, delta=None):
        with self.lock:
            v = self.player["volume"] + delta if delta is not None else vol
            v = max(0, min(100, int(v)))
            self.player["volume"] = v
        self.mpv.command("set_property", "volume", v)
        self.changed()

    def toggle_mute(self):
        with self.lock:
            muted = not self.player["muted"]
        self.mpv.command("set_property", "mute", muted)

    def seek(self, sec):
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

ACTIONS = {
    "start": lambda b: app.start(b.get("request", ""), b.get("opts") or {}),
    "next": lambda b: app.next(),
    "play": lambda b: app.play_index(int(b.get("index", -1))),
    "drop": lambda b: app.drop_index(int(b.get("index", -1))),
    "reshuffle": lambda b: app.reshuffle(),
    "stop": lambda b: app.stop(),
    "quit": lambda b: app.quit_player(),
    "browser": lambda b: app.open_browser(),
    "pause": lambda b: app.toggle_pause(),
    "mute": lambda b: app.toggle_mute(),
    "volume": lambda b: app.set_volume(b.get("vol"), b.get("delta")),
    "seek": lambda b: app.seek(float(b.get("sec", 0))),
    "max": lambda b: app.toggle_max(),
    "rate": lambda b: app.rate(b.get("ev")),
    "channel": lambda b: app.set_channel(bool(b.get("on"))),
    "q720": lambda b: app.set_q720(b.get("on")),
}


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
        q = parse_qs(urlparse(self.path).query)
        return secrets.compare_digest(q.get("t", [""])[0], TOKEN) or \
            secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._authed():
            return self._send(403, "合言葉（?t=）が違います", "text/plain; charset=utf-8")
        if path in ("/", "/index.html"):
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(app.snapshot(), ensure_ascii=False))
        elif path == "/events":
            self._events()
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
        except (ValueError, TypeError) as e:
            return self._send(400, json.dumps({"error": str(e)}))
        self._send(200, '{"ok":true}')

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
    log(f"http://localhost:{PORT}/?t={TOKEN} で待っています（Ctrl+C で終了）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.mpv.kill()


if __name__ == "__main__":
    main()
