# auto-selection（autotube）

観たいものを伝えると、Claude が YouTube 動画を選んで mpv で流し続けるスクリプトです。
テレビにつないだ Raspberry Pi 5（RAM 2GB）で、流しっぱなしにする用途で作りました。

```sh
autotube 将棋                                      # 選んで再生。終わったら次を選ぶ
autotube 将棋 でもいつもと違う感じ、棋士の素顔がわかるような  # 希望は文章でもよい
autotube 将棋のライブ中継                           # ライブ希望なら配信中のものから探す
autotube 猫 --live                                 # 配信中のものから探す（強制）
autotube 料理 --dry-run                            # 選ぶだけ（再生しない）
```

## Web リモコン（web.py）
再生はこれまでどおり mpv（テレビ側）で行い、操作をブラウザ（スマホ・PC）からできるようにしたものです。

```sh
python3 web.py        # http://<このマシン>:8766/ を開く
```

- 観たいものと検索条件（ライブを含む／長時間も含む／最新動画を優先）を入れて「スタート」
- 「ネクスト」に Claude が選んだ次の 5 本が並ぶ。▶ ですぐ再生、✕ で外す（自動で補充）、「選び直す」でまとめて入れ替え
- 一時停止・30 秒戻る・次へ・最大化の切り替え・👍👎、シークバーのクリックで移動
- **日本語の動画だけ**：選んだ動画の元の言語を動画ページで確かめ、日本語でなければ外して選び直す（検索結果のタイトルは日本語に機械翻訳されていることがあるため）。外したチャンネルは `data/foreign_channels.json` に残り、次から候補に入れない。「英語の」「海外の」と頼んだとき・音楽・チャンネル指定のときは絞らない
- **🎵 音楽だけ**：曲そのものが聴ける動画だけを選ぶ（解説・リアクションは外す。作業用 BGM・メドレー・アルバム丸ごとも可）
- **YouTube Music で選ぶ**：YouTube Music で曲を探し、その曲のラジオ（似た曲の自動プレイリスト）から Claude が選ぶ。曲名だけの検索結果より、アーティスト・長さの分かるラジオの方が選びやすいため
- **📻 ラジオ**：今の曲を起点にしたラジオから次を選ぶ（「このチャンネル」と同じく、押すと切り替わり、もう一度で解除）。起点の曲と、いま流れている曲のラジオを合わせて、流れから外れた曲は Claude が外す
- mpv は 1 つを常駐させ、JSON IPC（`--input-ipc-server`）で操作する。mpv のキーは `mpv-input-web.conf`（n=次へ、g=👍、b=👎、q=mpv を閉じる）
- 環境変数 `AUTOTUBE_HOST`（既定 `0.0.0.0`）・`AUTOTUBE_PORT`（既定 `8766`。8765 は phone-input が使う）。合言葉は既定では求めない（`?t=` なしで開ける）ので家の LAN の中だけで使う。求めるなら `AUTOTUBE_REQUIRE_TOKEN=1`（合言葉は phone-input の token と共通）

## しくみ
1. 希望を `claude -p`（既定は haiku）に渡し、切り口を変えた検索ワードを 3 個考えてもらう（ライブを観たいのかも判断する）
2. `yt-dlp` で検索して候補をまとめる（観た動画・2 分未満・10 時間超は除く）
3. Claude が候補から 1 本選び、理由を 1 行つける（ここまで約 15 秒）
4. mpv で全画面再生する。終わったら同じ希望で次を選ぶ

**選ぶときは、今回の希望が最優先です。** 好みメモ・👍👎・視聴履歴は、希望に合う候補の中で迷ったときの参考にするだけです。
過去の傾向に寄せすぎず、毛色の違う動画も選ばれます。

## 再生中のキー（mpv）
| キー | 動作 |
|---|---|
| n | 次の動画へ |
| q | 終了 |
| g | 👍（`data/history.jsonl` に記録） |
| b | 👎 |

mpv の通常のキー（スペースで一時停止、←/→ で 5 秒移動、f で全画面 など）もそのまま使えます。

## 必要なもの
- Python 3（標準ライブラリのみ）
- [Claude Code](https://claude.com/claude-code)（`claude` コマンド。ログイン済みであること）
- [mpv](https://mpv.io/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)（最新版を推奨。Debian の apt 版は古いことがある）と JS ランタイムの [deno](https://deno.com/)

## ファイル
| ファイル | 内容 |
|---|---|
| `autotube.py` | 本体。`~/.local/bin/autotube` などにリンクして使う |
| `web.py` | Web リモコンのサーバー（標準ライブラリのみ） |
| `web/index.html` | Web リモコンの画面 |
| `mpv-input.conf` | 再生中のキー割り当て |
| `mpv-input-web.conf` | Web 版の mpv のキー割り当て |
| `prefs.example.md` | 好みメモの雛形。初回起動時に `prefs.md` にコピーされる |
| `prefs.md` | 好みメモ（個人用。git 管理外） |
| `data/history.jsonl` | 再生と 👍👎 の記録（個人用。git 管理外） |

## 設定
- `AUTOTUBE_MODEL`：選ぶのに使うモデル（既定 `haiku`）
- 画質は 1080p までの H.264 を優先しています（Raspberry Pi 5 は H.264 をハードウェアで再生できず、AV1 より H.264 のほうが軽いため）。変えるときは `autotube.py` の `YTDL_FORMAT` を編集してください
- 配信形式は HLS（m3u8）を優先しています。通常の DASH 形式は mpv が直接取りに行くと 403 で拒否されることがあるためです
- WSL（WSLg）で動かすときは `--fs` の代わりに枠なしの画面サイズのウィンドウにします（`--fs` だとウィンドウが画面外に置かれて見えないため）。mpv の設定 `~/.config/mpv/mpv.conf` に `gpu-api=opengl` と `gpu-context=x11egl` も必要です
- SSH や tmux から起動したときは `WAYLAND_DISPLAY=wayland-0` を補って、テレビ側の画面に出します

## 今後のアイデア
### ABEMA の無料エピソードを探して流す
放送後 1 週間無料のアニメなどを、希望に合わせて Claude が選んで流す。2026-11-08 に次の部分まで動くことを確かめた（未実装）。

- **探す**：ブラウザ（Chrome / Chromium）を画面なしで動かして ABEMA のページを描かせ、表示された一覧を読む
  （`chrome --headless=new --virtual-time-budget=15000 --dump-dom https://abema.tv/video/genre/animation`）。
  カードのリンク（`/video/title/…`・`/video/episode/…`）と、ラベル（`ViewingTypeLabel__text--free` の「無料」「全話無料」「最新話無料」、有料は「プレミアム」）で見分けられる。検索ページも同じ方法で読めるはず（未確認）
- **再生する**：mpv（ffmpeg）は ABEMA の暗号化を扱えないので [Streamlink](https://streamlink.github.io/) を使う（`mise use -g pipx:streamlink`）。
  `streamlink -O https://abema.tv/video/episode/<ID> 720p | mpv -` で無料エピソードは再生でき、有料のものは Streamlink が「Premium stream is not playable」と弾く。
  web.py に組み込むなら `--player-external-http` で HTTP として出して mpv に `loadfile` させる
- 24 時間のアニメチャンネル（`https://abema.tv/now-on-air/abema-anime` など 26 個）も同じ方法で流せる
- ABEMA の非公開 API を直接使う方法（公式アプリ用の鍵を自作する）は使わない
- ABEMA の利用規約で自動での閲覧がどう扱われているかは未確認（規約ページが JavaScript で描かれていて取得できなかった）
