# サイトを足すときの確かめ方（autotube のリスト・ブラウザのプレーヤー）

ブラウザのプレーヤー（/player）は、どの動画も**埋め込み**で流す（YouTube は IFrame API、ほかはページか中のプレーヤーを iframe で）。
サイトごとの違いは 2 か所だけに書く。

- サーバーの側：[sites.py](sites.py)（落とすか・埋め込む URL・題名と画像）
- 拡張機能の側：`~/work/tonzura-config/brave/tv-fullscreen/sites.js`（字幕を隠す・案内を閉じる）

web.py・content.js の本体にはサイトの名前を書かない。

sites.py がページを読むときは、テレビの Brave と同じ名乗り（User-Agent、Brave の版から組み立てる）で読む。サイトが作る直リンクが名乗りに縛られていることがあるため（gogoanime の blogger は違う名乗りだと 403）。

## 分け方

まずは**プレーヤーの種類**で分ける（拡張機能が見分けるので、サイトの登録は要らない）。プレーヤーの種類で片付かない本当に特殊なサイトだけ、
sites.py・sites.js に個別に書く。

| プレーヤー | 見分け方 | 扱い |
|---|---|---|
| JW Player | `.jwplayer` の要素 | 字幕を切る・止める・飛ばす・音・速さ・位置を JW Player の決まった操作で（ページの中で `jwplayer()` を呼ぶ） |
| YouTube | `#movie_player`・`.ytp-*` の要素（ブラウザのプレーヤーでは IFrame API） | 止める・飛ばす・音・速さ・位置を YouTube の決まった操作で（JW Player と同じ形）。全画面ボタン・ライブのチャットを隠す・字幕を外す |
| それ以外 | — | `<video>` を直接 |

## チェックポイント

確かめ方（どのコマンドで何を見るか）は [CHECKS.md](CHECKS.md)。

新しいサイトのページをリストに足したら、上から順に確かめる。駄目だったところだけ直す。

| # | 確かめること | 見る場所 | 駄目なときの直し方 |
|---|---|---|---|
| 1 | 題名・サムネイルが出る | リモコンのリスト | `python3 sites.py URL` の「題名・画像」。og:title・og:image が無いサイトは sites.py に取り出し方を足す |
| 0 | 埋め込めないサイトは断る | リモコンの状態の行 | どうしても埋め込めなければ sites.py に `"unsupported": True`（リストに入れず「未対応です」） |
| 2 | 落とさない（ダウンロードが始まらない） | `~/.cache/autotube/media` が増えない | yt-dlp が対応しているサイトは落としてしまう。sites.py に `"download": False` |
| 3 | プレーヤーに埋め込まれて映る | テレビ | 映らない・真っ白：埋め込み禁止を拡張機能が外せていないか、ページが自分で抜け出している。そのサイトはタブで開く扱いにする |
| 4 | 動画の部分だけが映る（ページの広告や一覧が無い） | テレビ | ページの中の iframe（中のプレーヤー）を探して sites.py の `embed` に取り出し方を足す。`python3 sites.py URL` の「埋め込む」で確かめる |
| 5 | 自動で始まる | テレビ・音 | 埋め込んだあと 5〜21 秒に `tvfs kick` を送っている。始まらなければ、そのプレーヤーの大きな再生ボタンの class を content.js の kick の一覧に足す |
| 6 | 音が出る | 音 | Brave の自動再生のポリシー（`/etc/brave/policies/managed/tonzura-autoplay.json`）。ミュートで始まるプレーヤーは `tvfs video unmute` |
| 7 | F9 で動画だけが画面いっぱい | テレビ | 拡張機能の fill。広がらなければ、動画の親の CSS（transform など）を見る |
| 8 | 別のタブへ移って戻っても全画面のまま | テレビ | プレーヤーの wantFs（戻ったら web.py が本物の F9 を送る） |
| 9 | 字幕（焼き込みでないもの）が出ない | テレビ | JW Player なら自動で切れる（決まった操作 setCurrentCaptions(0)）。それ以外は sites.js の `hideSubs` に字幕の CSS。F6＋C で出し入れ |
| 10 | 案内・ポップアップが邪魔しない | テレビ | sites.js の `dismiss` に閉じるボタンの文字 |
| 11 | 終わったら次へ進む（もう 1 本飛ばされない） | 最後の数秒へ飛ばす（`tvfs video pct:99`）。CHECKS.md の 6 | 拡張機能が ended・残り 20 秒以内で 5 秒止まった・埋め込みの中の URL が変わったのを見て autotube の次へ。進まなければ ext.log の「autotube の次へ」を見る |

## 今の登録

| サイト | 落とす | 埋め込むもの | 拡張機能の癖 |
|---|---|---|---|
| YouTube | 落とさない（IFrame API。埋め込み禁止のときだけ落とす） | 埋め込み用の URL | 字幕を外す（プレーヤー側）・ライブのチャットを隠す |
| ABEMA | 落とさない | ページごと | 「好きなジャンルを選んで」を閉じる |
| gogoanime | 落とさない | 中のプレーヤー（data-src → megaplay.su。source=blogger は googlevideo の直リンクで映らないので後回し） | なし（JW Player として扱う。英語の字幕は映像に焼き込み。JW Player の字幕の一覧は Off だけ） |
| KissAnime | 落とさない | 中のプレーヤー（gogo-player.php → megaplay.buzz） | なし（JW Player として扱う。自作の ASS 字幕も JW Player の操作で消えた。CSS・メニューを押すのは効かなかった） |
| ニコニコ動画 | **未対応**（リストに足そうとすると「未対応です」と断る。sites.py の `unsupported`） | —（埋め込むと動画が読み込まれない。視聴ページも埋め込み用のプレーヤーも。クッキーを許しても同じ。普通のタブでは流れる） | — |
| それ以外 | yt-dlp で流せれば落とす。流せなければページごと埋め込む | ページごと | なし |
