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
| `mpv-input.conf` | 再生中のキー割り当て |
| `prefs.example.md` | 好みメモの雛形。初回起動時に `prefs.md` にコピーされる |
| `prefs.md` | 好みメモ（個人用。git 管理外） |
| `data/history.jsonl` | 再生と 👍👎 の記録（個人用。git 管理外） |

## 設定
- `AUTOTUBE_MODEL`：選ぶのに使うモデル（既定 `haiku`）
- 画質は 1080p までの H.264 を優先しています（Raspberry Pi 5 は H.264 をハードウェアで再生できず、AV1 より H.264 のほうが軽いため）。変えるときは `autotube.py` の `YTDL_FORMAT` を編集してください
- SSH や tmux から起動したときは `WAYLAND_DISPLAY=wayland-0` を補って、テレビ側の画面に出します
