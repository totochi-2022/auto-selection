"""サイトごとの扱い（autotube のリスト・ブラウザのプレーヤー）。サイトの名前が出てくる処理はここに集める。

新しいサイトを足すときの確かめ方は SITES.md。`python3 sites.py URL` で、そのサイトがどう扱われるか（題名・画像・
埋め込む URL・落とすか）を表示できる。拡張機能の側のサイトの癖は ~/work/tonzura-config/brave/tv-fullscreen/sites.js。
"""
import html
import re
import sys
import urllib.request
from urllib.parse import urljoin, urlparse



def _browser_ua():
    """テレビの Brave と同じ名乗り（User-Agent）。サイトが作る動画の直リンクは、ページを読んだときの名乗りでないと
    断られることがある（gogoanime の blogger は googlevideo の直リンクが名乗りに縛られ、Brave で開くと 403 だった）。
    Brave の名乗りは Chrome と同じ形で、版の番号は brave-browser --version の最初の数字（157.1.99.57 なら 157）。
    Brave が無い PC（Pi）では素っ気ない名乗りのまま"""
    import subprocess
    try:
        v = subprocess.run(["brave-browser", "--version"], capture_output=True, text=True, timeout=10).stdout
        major = re.search(r"(\d+)\.\d+\.\d+\.\d+", v).group(1)
    except (OSError, subprocess.SubprocessError, AttributeError):
        return "Mozilla/5.0"
    return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


UA = _browser_ua()
YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")


def _get(url, referer=None, limit=600000):
    h = {"User-Agent": UA, **({"Referer": referer} if referer else {})}
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=15).read(limit).decode("utf-8", "replace")


def page_meta(url):
    """ページの題名（og:title か <title>）と画像（og:image）。yt-dlp で調べられないページ用"""
    try:
        page = _get(url, limit=400000)
    except (OSError, ValueError):
        return {}

    def og(name):
        m = re.search(rf'<meta[^>]+property=["\']og:{name}["\'][^>]+content=["\']([^"\']+)', page) \
            or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{name}["\']', page)
        return html.unescape(m.group(1)).strip() if m else ""
    t = og("title")
    if not t:
        m = re.search(r"<title[^>]*>([^<]+)", page)
        t = html.unescape(m.group(1)).strip() if m else ""
    return {"title": t, "image": og("image")}


# ---- 中のプレーヤー（動画の部分の iframe）の取り出し方。期限付きのことがあるので残さず、流すたびに取り出す ----
def _youtube(url):
    m = YT_ID.search(url)
    return f"https://www.youtube.com/embed/{m.group(1)}?autoplay=1" if m else None


def _gogoanime(url):
    """gogoanime：ページの data-src（/player/?…）→ その中の megaplay などの URL"""
    host = urlparse(url).hostname
    ref = f"https://{host}/"
    players = [html.unescape(p) for p in re.findall(r'data-src="(https://[^"]+/player/\?[^"]+)"', _get(url, ref))]
    if not players:
        return None
    # source=blogger は中が googlevideo の直リンク（取りに行った側の IP に縛られ、テレビでは映らない）なので後回し
    players.sort(key=lambda p: "source=blogger" in p)
    for player in players:
        for mi in re.finditer(r'https://[\w.-]+/(?:embed|e|v|stream)[^"\'<>\s]*', _get(player, ref)):
            if "googlevideo.com" not in mi.group(0):
                return mi.group(0)
    return players[0]  # 中が見つからなければ gogoanime のプレーヤーのページごと


def _kissanime(url):
    """KissAnime：ページの iframe（/gogo-player.php?…）→ その中の megaplay の URL"""
    host = urlparse(url).hostname
    ref = f"https://{host}/"
    m = re.search(r'<iframe[^>]+src="([^"]*gogo-player\.php[^"]*)"', _get(url, ref))
    if not m:
        return None
    player = urljoin(url, html.unescape(m.group(1)))
    mi = re.search(r'https://[\w.-]+/(?:stream|embed|e|v)/[^"\'<>\s]*', _get(player, ref))
    return mi.group(0) if mi else player


def _niconico(url):
    """ニコニコ動画：公式の埋め込み用のプレーヤー（embed.nicovideo.jp）"""
    m = re.search(r"/watch/((?:sm|so|nm)?\d+)", url)
    return f"https://embed.nicovideo.jp/watch/{m.group(1)}?autoplay=1" if m else None


# hosts：ホスト名に含まれる文字。download：False なら yt-dlp で落とせても落とさず埋め込む。embed：中のプレーヤーの取り出し方
SITES = [
    {"name": "YouTube", "hosts": ("youtube.com", "youtu.be"), "embed": _youtube},
    {"name": "ABEMA", "hosts": ("abema.tv",), "download": False},  # 見逃しの回は 20 分で 700MB ほど落としていた
    {"name": "gogoanime", "hosts": ("gogoanime",), "download": False, "embed": _gogoanime},
    # ニコニコ動画：未対応。埋め込み用のプレーヤー（_niconico）は「エラーが発生しました」、視聴ページを埋め込むと映るがコメントだけで
    # 動画が読み込まれない（readyState 0）。Brave のポリシー（tonzura-embed-cookies.json）と盾でクッキーを許しても同じ。
    # 普通のタブでは両方とも流れるので、埋め込まれていると断られている。リストには入れない（yt-dlp で落とせばコメントなしで流せる）
    {"name": "ニコニコ動画", "hosts": ("nicovideo.jp", "nico.ms"), "unsupported": True},
    # Prime Video：未対応。埋め込むとログインが引き継がれず（ページはログインしていない扱い）、裏で流れる予告（2 分ほど）しか映らない。
    # 本編は DRM で、埋め込みの中ではまず流れない。予告が流れるので見張りでは飛ばず、2 分ほど挟まっていた（2026-11-21）
    {"name": "Prime Video", "hosts": ("primevideo.com", "amazon.co.jp", "amazon.com"), "unsupported": True},
    {"name": "KissAnime", "hosts": ("kissanime",), "download": False, "embed": _kissanime},
]


def find(url):
    host = urlparse(url).hostname or ""
    return next((s for s in SITES if any(h in host for h in s["hosts"])), None)


def unsupported(url):
    """リストに入れないサイトなら断る文、入れてよければ空"""
    s = find(url)
    return f"{s['name']}は未対応です" if s and s.get("unsupported") else ""


def no_download(url):
    s = find(url)
    return bool(s) and s.get("download") is False


def gone(url):
    """ページが消えていれば理由（404・410 など）、あれば空。調べられないとき（つながらない・時間切れ）も空（流してみる）"""
    import urllib.error
    if YT_ID.search(url):
        return ""  # YouTube はプレーヤーのエラーの番号で分かる
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=10)
        final = r.geturl()
        r.close()
        # 消えた回のページはトップページへ転送されることが多い（gogoanime・KissAnime は 200 で / へ）
        if urlparse(url).path.strip("/") and not urlparse(final).path.strip("/"):
            return "ページが消えている（トップページへ転送された）"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410, 451):
            return f"ページが消えている（{e.code}）"
    except (OSError, ValueError):
        pass
    return ""


def embed_url(url, log=print):
    """プレーヤーに埋め込む URL。中のプレーヤーが取れなければページごと"""
    s = find(url)
    if s and s.get("embed"):
        try:
            return s["embed"](url) or url
        except (OSError, ValueError) as e:
            log(f"埋め込みの URL を取れなかった（{s['name']}）: {url} {e}")
    return url


if __name__ == "__main__":
    for u in sys.argv[1:]:
        s = find(u)
        print(f"URL     : {u}\nサイト  : {s['name'] if s else '（登録なし：ページごと埋め込む）'}")
        if unsupported(u):
            print(f"未対応  : {unsupported(u)}（リストに足せない）")
        print(f"落とすか: {'落とさない（埋め込む）' if no_download(u) else 'yt-dlp で流せれば落とす（YouTube は埋め込み）'}")
        m = page_meta(u)
        print(f"消えたか: {gone(u) or 'ある'}")
        print(f"題名    : {m.get('title')}\n画像    : {m.get('image')}\n埋め込む: {embed_url(u)}\n")
