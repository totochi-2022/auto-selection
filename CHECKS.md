# 動作チェックのやり方（テレビ・autotube のプレーヤー・拡張機能）

Claude がテレビ（tonzura）で自動で確かめるための手順。調べる道具は `tvprobe`
（`~/work/tonzura-config/home/.local/bin/tvprobe`、`tvprobe` だけで使い方が出る）。
新しいサイトを足すときの確かめる項目は [SITES.md](SITES.md) のチェックポイント。ここは「どうやって確かめるか」。

**テレビを実際に動かすので、ユーザーが見ていないとき・頼まれたときに流す。** 終わったら元のタブ・元の動画に戻す。

## 道具

| やりたいこと | コマンド | 読み方 |
|---|---|---|
| 再生中か | `tvprobe moving` | 画面の真ん中を 1.5 秒あけて 2 回撮る。`moving`＝動いている、`still`＝止まっている（静止画の多い動画は再生中でも still。位置と音で補う） |
| 再生の位置・止まっているか | `tvprobe player` | autotube の今の動画・位置・paused・muted・リスト。2 回見て位置が進めば再生中 |
| 音が出ているか | `tvprobe rms` | テレビ（DAC）に出ている音を 2 秒録った大きさ。普段 1500〜2600 ほど、ミュート・無音は 0。無音の場面と取り違えないよう数秒あけて 2〜3 回 |
| どの音が出ているか | `tvprobe streams` | 音の流れごとの大きさ。YouTube の埋め込みと吹き替えの声は別の流れ（2 つ）になる |
| 画面を見る | `tvprobe shot 名前` | PNG の場所が出るので Read で見る |
| 手前のタブ | `tvprobe tab` | URL と題名 |
| autotube が何をしたか | `tvprobe weblog` | web.py の記録（`~/.cache/autotube/web.log`）。流せなかった理由など |
| 拡張機能が何をしたか | `tvprobe log` | 「JW Player の操作」「autotube の次へ」「字幕を切った」「案内を閉じた」など |
| 次へ進んだか | `tvprobe wait-next 60` | 今の動画が変わるまで待つ |

操作を送る側の道具

| 操作 | コマンド |
|---|---|
| 動画の操作（F6＋キーと同じ中身） | `tvfs video toggle／mute／volup／voldown／fwd／back／fwd60／back180／pct:99 …` |
| 埋め込みのあと「始まっていなければ再生」 | `tvfs kick` |
| 全画面（本物の F9 キー） | `pkey f9` |
| タブを移る | `tvfs tab term／player／browse` |
| autotube に足す・流す | `curl -X POST http://127.0.0.1:8766/api/add -H 'Content-Type: application/json' -d '{"url":"…","play":true}'`（play を外すとリストの最後に足すだけ） |

## 項目ごとの確かめ方

### 1. 自動で始まる
1. `api/add` に `"play":true` で流す。
2. 20 秒ほど待ってから `tvprobe moving` と `tvprobe rms`。
3. 始まらなければ `tvprobe log` を見る。埋め込みは 5〜21 秒に `tvfs kick` を送っている。

### 2. 音・ミュート・音量
1. `tvprobe rms` で今の大きさ。
2. `tvfs video mute` → 1.5 秒 → `tvprobe rms` が 0 か。
3. もう一度 `tvfs video mute` → 元の大きさに戻るか。
4. `tvfs video voldown`・`volup`：`tvprobe log` の「音量 89%」など。
- YouTube・JW Player は決まった操作で動く（`tvprobe log` に「JW Player の操作」と出る。YouTube も同じ見出し）。動画を直接ミュートすると、プレーヤーが戻すことがある（YouTube で 2562 のまま効かなかった）。

### 3. 一時停止・進む・戻る
1. `tvprobe player` で位置。
2. `tvfs video fwd60` → 3 秒 → 位置が約 60 秒進んだか。`back` で約 10 秒戻るか。
3. `tvfs video toggle` → `tvprobe player` の paused が True。もう一度で False。

### 4. 全画面（動画だけが画面いっぱい）
1. `pkey f9` → 3 秒 → `tvprobe shot fs1` を見る。タブ・アドレスバー・ページの一覧が写っていなければ OK（上に「全画面表示を終了するには Esc」の帯が出る）。
2. 埋め込みのページごと全画面になっていたら NG（拡張機能の fill が効いていない）。
3. `tvfs tab term` → 2 秒 → `tvfs tab player` → 4 秒 → `tvprobe shot fs2`：全画面に戻っているか。
4. もう一度 `pkey f9` で元に戻るか。

### 5. 字幕
1. セリフのある所で `tvprobe shot sub` を見る。画面の下に字幕の文字が無いか。
2. 決まった秒数が無ければ、`tvfs video pct:20`・`pct:40`… と飛ばして何枚か撮る。
3. `tvprobe log` に「JW Player の字幕を切った」が出ているか（JW Player のとき）。

### 6. 終わったら次へ
1. リストに次を 1 本足しておく（`play` なしの `api/add`）。
2. `tvfs video pct:99` で最後へ。
3. `tvprobe wait-next 60` で次へ進むか。
4. `tvprobe log` の「autotube の次へ」の理由（ended・最後で止まった・埋め込みの中のページが次へ移った）。
- ABEMA は最後の 14 秒ほどで止まる（残り 20 秒以内で 5 秒止まったら終わりとみなしている）。

### 7. 落としていない
- `ls ~/.cache/autotube/media` が増えていないか、`pgrep -af yt-dlp` で何も動いていないか。

### 8. 吹き替え
1. 吹き替えのある動画（台本が `~/.local/share/autotube/dub-scripts/` にあるもの）を流す。
2. `tvprobe streams` で Brave の流れが 2 つあり、両方に音があるか。
3. `tvprobe shot dub` で日本語の字幕（吹き替えの文）が出ているか。

### 9. 流せなかったものを飛ばす
見本（2026-11-20 時点。状況が変わるので、効かなくなったら探し直す）

| 種類 | URL | 期待すること |
|---|---|---|
| 消えたページ | https://abema.tv/video/episode/189-37_s1_p1（ほかに 15-1tnnwtcfxhd_s0_p1・19-81_s1_p1） | 404 なので埋め込まずにすぐ飛ばす |
| 消えた回（アニメのサイト） | https://gogoanime.by/sekai-saikyou-no-majo-hajimemashita-episode-99-english-subbed/（無い回の番号で作れる。KissAnime も同じ） | 200 でトップページへ転送されるので、それを消えたとみなしてすぐ飛ばす |
| YouTube のメンバー限定 | https://www.youtube.com/watch?v=Pw0wchRQJac | エラー 150 か「始まる前に終わった」で飛ばす |
| 動画の無いページ | https://example.com/ | 45 秒で飛ばす |
| ABEMA の有料（先行配信） | https://abema.tv/video/episode/210-18_s1_p21 | 45 秒で飛ばす（ページの印は free:false・someFree:true） |
| ABEMA の有料（通常） | https://abema.tv/video/episode/210-18_s1_p5 | 45 秒で飛ばす |
| （有料の印でも流れるもの） | https://abema.tv/video/episode/536-1_s1_p15 | ページの印は free:false だが、無料の期間で流れる。ページの印だけで有料と決めない理由 |

1. 次に正常なもの（例：gogoanime）を `api/add` で足してから、見本を `"play":true` で流す。
2. `tvprobe wait-next 100` で次へ進むか。消えたページは数秒、動画の無いページは 45〜50 秒。
3. `tvprobe weblog` に「流せなかったので飛ばします：… 理由」。履歴（`data/history.jsonl`）に `"event": "failed"` と理由。
4. 正常なものが誤って飛ばされないか：正常なものを 1 分流して、`tvprobe weblog` に飛ばした記録が出ないこと。
- 仕組み：消えたページは sites.py の `gone`（404・410・451、トップページへの転送）。YouTube はエラーの番号（2・5・100 は消えた、それ以外は落として流し、落とせなければ飛ばす）。
  始まらないものは、流し始めて 45 秒のうちに「再生が進んだ」知らせ（埋め込みは拡張機能の alive、YouTube とファイルはプレーヤーの位置）が無ければ飛ばす。

## 2026-11-20 に一通り試した結果

| サイト | 自動で始まる | 音 | ミュート | 全画面 | 戻っても全画面 | 字幕を切る | 終わったら次へ |
|---|---|---|---|---|---|---|---|
| YouTube（埋め込み） | ○ | ○ | ○（直した） | ○ | ○ | ○（プレーヤーで外す） | ○ |
| ABEMA | ○ | ○ | 未 | ○ | ○ | — | ○ |
| gogoanime | ○ | ○ | 未 | ○ | 未 | ×（焼き込み） | ○ |
| KissAnime | ○ | ○ | ○ | 未 | 未 | ○ | ○ |

まだ試していないこと：F6 を押しながらのキー（本物のキーの組み合わせ。`pkey` は修飾キーしか押したままにできない）。中の処理は `tvfs video` と同じ。
