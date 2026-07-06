"""
core/fanza_client.py — FANZA データ取得 & 商品スコアリング

スコアリング戦略:
  高割引 > 新作 > 人気女優 > ジャンル優先度
セール商品は最優先（割引率に比例したボーナス付与）
"""
import sys
import re
import time
import random
import logging
import urllib.request
import urllib.parse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import FANZA, SCORING, DOUJIN

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

log = logging.getLogger(__name__)


def _safe_int_price(value, default: int = 0) -> int:
    """
    FANZA APIの価格フィールドをintに変換する。
    複数オプション商品は "350~" のような範囲表記になることがあるため、
    先頭の数字部分だけを取り出す。
    """
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    match = re.match(r"\d+", str(value))
    return int(match.group()) if match else default

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


# ────────────────────────────────────────────
# サンプル動画の実ファイルURL解決・ダウンロード
# ────────────────────────────────────────────
_AGE_CHECK_COOKIES = {"age_check_done": "1"}


def resolve_sample_video_url(sample_movie_url: str):
    """
    sample_movie_url（litevideo/partページ）→ iframe(html5_player) → 実mp4URL
    の順にページを辿って、実際にダウンロード可能なmp4の直リンクを取得する。
    取得できなければ None を返す。
    """
    if not HAS_REQUESTS or not sample_movie_url:
        return None
    try:
        resp = requests.get(
            sample_movie_url, headers=_REQUEST_HEADERS,
            cookies=_AGE_CHECK_COOKIES, timeout=15,
        )
        resp.raise_for_status()
        m = re.search(r'<iframe[^>]+src="([^"]+html5_player[^"]+)"', resp.text)
        if not m:
            log.warning("[動画] html5_player iframeが見つかりません: %s", sample_movie_url)
            return None
        player_url = m.group(1).replace("&amp;", "&")

        resp2 = requests.get(
            player_url, headers=_REQUEST_HEADERS,
            cookies=_AGE_CHECK_COOKIES, timeout=15,
        )
        resp2.raise_for_status()
        m2 = re.search(r'"src":"([^"]+\.mp4)"', resp2.text)
        if not m2:
            log.warning("[動画] mp4 srcが見つかりません: %s", player_url)
            return None

        video_url = m2.group(1).replace("\/", "/")
        if video_url.startswith("//"):
            video_url = "https:" + video_url
        return video_url
    except Exception as e:
        log.warning("[動画] サンプル動画URL解決失敗: %s", e)
        return None


def download_sample_video(sample_movie_url: str, dest_path) -> bool:
    """実際のサンプル動画(mp4)をダウンロードして dest_path に保存する"""
    video_url = resolve_sample_video_url(sample_movie_url)
    if not video_url:
        return False
    try:
        resp = requests.get(
            video_url, headers=_REQUEST_HEADERS,
            cookies=_AGE_CHECK_COOKIES, timeout=60, stream=True,
        )
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
        log.info("[動画] サンプル動画ダウンロード完了: %s", dest_path)
        return True
    except Exception as e:
        log.warning("[動画] サンプル動画ダウンロード失敗: %s", e)
        return False


# ────────────────────────────────────────────
# FANZA API v3 クライアント
# ────────────────────────────────────────────
class FanzaAPIClient:
    BASE = "https://api.dmm.com/affiliate/v3"

    def __init__(self, api_id: str = "", affiliate_id: str = ""):
        self.api_id       = api_id or FANZA["api_id"]
        self.affiliate_id = affiliate_id or FANZA["affiliate_id"]

    def _get(self, endpoint: str, params: dict) -> dict:
        params.update({
            "api_id":       self.api_id,
            "affiliate_id": self.affiliate_id,
            "output":       "json",
        })
        url = f"{self.BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url, headers=_REQUEST_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            log.error("FANZA API 呼び出し失敗 [%s]: %s", endpoint, e)
            return {}

    def get_items(self, genre_key: str, sort: str = "rank", hits: int = 20, genre_id=None) -> list[dict]:
        params = {
            "site":    "FANZA",
            "service": "digital",
            "floor":   "videoa",
            "hits":    hits,
            "sort":    sort,
        }
        # FANZA APIの genre=<文字列> は無視されるため、実際の数値ジャンルIDで絞り込む
        if genre_id:
            params["article"]    = "genre"
            params["article_id"] = genre_id
        data = self._get("ItemList", params)
        items = data.get("result", {}).get("items", [])
        return [self._normalize(item) for item in items]

    def get_new_releases(self, genre_key: str, hits: int = 10, genre_id=None) -> list[dict]:
        return self.get_items(genre_key, sort="date", hits=hits, genre_id=genre_id)

    def search_actress_id(self, actress_name: str) -> Optional[str]:
        """
        女優名からFANZA内部の女優IDを検索して返す。
        ItemList の actress パラメータに渡す数値IDが必要なため。
        見つからなければ None。
        """
        data = self._get("ActressSearch", {
            "site":    "FANZA",
            "keyword": actress_name,
            "hits":    5,
        })
        actresses = data.get("result", {}).get("actress", [])
        if not actresses:
            log.warning("[女優検索] 見つかりません: %s", actress_name)
            return None

        # 名前が完全一致するものを優先、なければ先頭
        for a in actresses:
            if a.get("name") == actress_name:
                return str(a.get("id", ""))
        return str(actresses[0].get("id", ""))

    def get_actress_products(
        self,
        actress_name: str,
        sort: str = "rank",
        hits: int = 20,
    ) -> list[dict]:
        """
        指定女優の出演作品をサンプル動画あり限定で取得する。
        FANZA API の actress パラメータは数値IDで指定する仕様。
        """
        actress_id = self.search_actress_id(actress_name)
        if not actress_id:
            return []

        log.info("[女優] %s (id=%s) の作品を取得", actress_name, actress_id)
        data = self._get("ItemList", {
            "site":    "FANZA",
            "service": "digital",
            "floor":   "videoa",
            "hits":    hits,
            "sort":    sort,
            "actress": actress_id,
        })
        items = data.get("result", {}).get("items", [])
        normalized = [self._normalize(item) for item in items]

        # サンプル動画あり限定
        return [i for i in normalized if i.get("sample_movie_url")]

    def get_doujin_items(self, sort: str = "rank", hits: int = 20) -> list[dict]:
        """
        FANZA同人誌（電子書籍）のランキング上位を取得する。
        floor=digital_doujin は動画とはfloorが異なり、
        サンプル画像（sampleImageURL）はあるがサンプル動画は存在しない。
        """
        data = self._get("ItemList", {
            "site":    "FANZA",
            "service": "doujin",
            "floor":   "digital_doujin",
            "hits":    hits,
            "sort":    sort,
        })
        items = data.get("result", {}).get("items", [])
        return [self._normalize_doujin(item) for item in items]

    def _normalize_doujin(self, raw: dict) -> dict:
        prices = raw.get("prices", {})
        info   = raw.get("iteminfo", {})

        sample_images = (
            raw.get("sampleImageURL", {}).get("sample_l", {}).get("image")
            or raw.get("sampleImageURL", {}).get("sample_s", {}).get("image")
            or []
        )

        # 同人誌は「出演女優」の代わりに作者/サークル名を actress 欄に流用する
        # （Gemini投稿文生成・エンゲージメント統計が女優名と同じ仕組みで使い回せる）
        creator = ",".join(a.get("name", "") for a in info.get("author", []))
        if not creator:
            creator = (info.get("maker") or [{}])[0].get("name", "")

        return {
            "product_id":      raw.get("content_id", ""),
            "title":           raw.get("title", ""),
            "actress":         creator,
            "genres":          ",".join(g.get("name", "") for g in info.get("genre", [])),
            "maker":           (info.get("maker") or [{}])[0].get("name", ""),
            "label":           (info.get("label") or [{}])[0].get("name", ""),
            "release_date":    raw.get("date", ""),
            "minutes":         0,
            "affiliate_url":   raw.get("affiliateURL", ""),
            "product_url":     raw.get("URL", ""),
            "thumbnail_url":   raw.get("imageURL", {}).get("large") or raw.get("imageURL", {}).get("list", ""),
            "sample_movie_url": "",              # 同人誌にサンプル動画はない
            "sample_image_urls": sample_images,  # サンプル画像（投稿用は先頭を使用）
            "price":           _safe_int_price(prices.get("price")),
            "list_price":      _safe_int_price(prices.get("list_price") or prices.get("listprice")),
        }

    def _normalize(self, raw: dict) -> dict:
        prices       = raw.get("prices", {})
        info         = raw.get("iteminfo", {})
        sample_movie = raw.get("sampleMovieURL", {})

        # サンプル動画URLは画質の高い順に取得
        sample_movie_url = (
            sample_movie.get("size_720_480")
            or sample_movie.get("size_560_360")
            or sample_movie.get("size_476_306")
            or ""
        )

        return {
            "product_id":      raw.get("content_id", ""),
            "title":           raw.get("title", ""),
            "actress":         ",".join(a.get("name", "") for a in info.get("actress", [])),
            "genres":          ",".join(g.get("name", "") for g in info.get("genre", [])),
            "maker":           (info.get("maker") or [{}])[0].get("name", ""),
            "label":           (info.get("label") or [{}])[0].get("name", ""),
            "release_date":    raw.get("date", ""),
            "minutes":         int(raw.get("volume", 0) or 0),
            "affiliate_url":   raw.get("affiliateURL", ""),
            "product_url":     raw.get("URL", ""),          # FANZA商品ページURL
            "thumbnail_url":   raw.get("imageURL", {}).get("list", ""),
            "sample_movie_url": sample_movie_url,           # サンプル動画URL
            "price":           _safe_int_price(prices.get("price")),
            "list_price":      _safe_int_price(prices.get("listprice")),
        }


# ────────────────────────────────────────────
# スクレイピングフォールバック
# ────────────────────────────────────────────
class FanzaScraper:
    """APIキーなしでランキングページをスクレイピング"""

    RANKING_URL = "https://www.dmm.co.jp/digital/videoa/-/ranking/=/genre={genre}/"

    def get_items(self, genre_key: str, limit: int = 20) -> list[dict]:
        if not HAS_REQUESTS:
            log.warning("requests未インストール。スクレイピング不可。")
            return []

        url = self.RANKING_URL.format(genre=genre_key)
        try:
            resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=15)
            resp.raise_for_status()
            return self._parse(resp.text, genre_key, limit)
        except Exception as e:
            log.error("スクレイピング失敗 [%s]: %s", genre_key, e)
            return []
        finally:
            time.sleep(random.uniform(2.0, 4.0))

    def _parse(self, html: str, genre_key: str, limit: int) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        results = []

        for rank, item in enumerate(soup.select("li.ranking-item")[:limit], 1):
            title_el = item.select_one(".title")
            price_el = item.select_one(".price")
            link_el  = item.select_one("a")
            img_el   = item.select_one("img")
            results.append({
                "product_id":    f"sc_{genre_key}_{rank:04d}_{int(time.time())}",
                "title":         title_el.get_text(strip=True) if title_el else "不明",
                "actress":       "",
                "genres":        genre_key,
                "maker":         "",
                "label":         "",
                "release_date":  "",
                "minutes":       0,
                "affiliate_url": link_el.get("href", "") if link_el else "",
                "thumbnail_url": img_el.get("src", "") if img_el else "",
                "price":         _parse_price(price_el.get_text() if price_el else "0"),
                "list_price":    0,
            })
        return results


def _parse_price(text: str) -> int:
    digits = "".join(c for c in text if c.isdigit())
    return int(digits) if digits else 0


# ────────────────────────────────────────────
# デモ用ダミーデータ
# ────────────────────────────────────────────
_DEMO_TITLES = [
    "背徳の人妻 〜夫の目を盗んで〜",
    "熟れた果実 五十路の誘惑",
    "隣の奥さんはH好き",
    "人妻秘密の昼下がり",
    "寝取られ願望 〜妻を差し出した夜〜",
    "NTR記録映像 第三者視点",
    "素人ナンパ 渋谷センター街",
    "ハメ撮り専門 本物素人",
    "若妻の秘密 結婚3年目の告白",
    "熟女倶楽部 会員制サロン潜入",
    "巨乳妻の誘惑 Hカップの秘密",
    "中出し懇願 奥まで欲しいの",
]
_DEMO_ACTRESSES = [
    "三浦恵理子", "麻生希", "友田彩也香", "吉沢明歩", "波多野結衣",
    "上原亜衣", "紗倉まな", "松下紗栄子", "夏目彩春", "川上奈々美",
]
_DEMO_MAKERS = ["madonna", "MOODYZ", "プレステージ", "S1", "WANZ FACTORY"]


def make_demo_items(genre_key: str, count: int = 20) -> list[dict]:
    rng = random.Random(f"{genre_key}{datetime.now().strftime('%Y%m%d')}")
    items = []
    for i in range(count):
        base_price = rng.choice([3990, 4990, 5990, 6990])
        on_sale    = rng.random() < 0.35
        price      = int(base_price * rng.uniform(0.4, 0.7)) if on_sale else base_price
        pid        = f"demo_{genre_key}_{i+1:03d}"
        release_days_ago = rng.randint(0, 90)
        release_date = (datetime.now() - timedelta(days=release_days_ago)).strftime("%Y-%m-%d")

        # デモ: 約70%の商品にサンプル動画あり
        has_sample = rng.random() < 0.70
        sample_url = f"https://cc3001.dmm.co.jp/litevideo/freepv/{pid[:3]}/{pid[:7]}/{pid}/demo.mp4" if has_sample else ""

        items.append({
            "product_id":      pid,
            "title":           rng.choice(_DEMO_TITLES),
            "actress":         rng.choice(_DEMO_ACTRESSES),
            "genres":          f"{genre_key},{rng.choice(['中出し','巨乳','美乳'])}",
            "maker":           rng.choice(_DEMO_MAKERS),
            "label":           "",
            "release_date":    release_date,
            "minutes":         rng.choice([90, 120, 150, 180]),
            "affiliate_url":   f"https://www.dmm.co.jp/demo/{pid}/?lurl=affiliate",
            "product_url":     f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={pid}/",
            "thumbnail_url":   "",
            "sample_movie_url": sample_url,
            "has_sample_movie": 1 if has_sample else 0,
            "price":           price,
            "list_price":      base_price,
        })
    return items


# ────────────────────────────────────────────
# 女優特化用ユーティリティ
# ────────────────────────────────────────────
def _make_actress_demo_items(actress_name: str, count: int = 10) -> list[dict]:
    """女優特化のデモデータ（全作品にその女優が出演）"""
    rng = random.Random(f"{actress_name}{datetime.now().strftime('%Y%m%d')}")
    items = []
    for i in range(count):
        base_price = rng.choice([3990, 4990, 5990])
        on_sale    = rng.random() < 0.3
        price      = int(base_price * rng.uniform(0.5, 0.7)) if on_sale else base_price
        pid        = f"demo_actress_{i+1:03d}"
        days_ago   = rng.randint(0, 180)
        release_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        sample_url = f"https://cc3001.dmm.co.jp/litevideo/freepv/a/act/{pid}/demo.mp4"

        items.append({
            "product_id":      pid,
            "title":           f"{actress_name} {rng.choice(_DEMO_TITLES)}",
            "actress":         actress_name,
            "genres":          rng.choice(["人妻,中出し", "熟女,巨乳", "素人,ハメ撮り"]),
            "maker":           rng.choice(_DEMO_MAKERS),
            "label":           "",
            "release_date":    release_date,
            "minutes":         rng.choice([90, 120, 150]),
            "affiliate_url":   f"https://www.dmm.co.jp/demo/{pid}/?lurl=affiliate",
            "product_url":     f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={pid}/",
            "thumbnail_url":   "",
            "sample_movie_url": sample_url,
            "has_sample_movie": 1,
            "price":           price,
            "list_price":      base_price,
        })
    return items


_DEMO_DOUJIN_TITLES = [
    "隣の人妻先輩と密着残業",
    "幼馴染とお風呂場で〜",
    "退魔士少女の敗北エッチ",
    "催眠アプリで学校中の女子を〜",
    "オフィスレディの寝取られ報告書",
    "ふたなり女教師の放課後補習",
    "田舎に帰省したら叔母が〜",
    "冒険者ギルドの淫らな依頼",
    "サキュバスカフェへようこそ",
    "妹の友達が家に泊まりに来た話",
]
_DEMO_CIRCLES = [
    "サークルほっと茶", "深夜の工房", "ピンク工廠", "蜜柑堂",
    "ノラネコ製作所", "夢幻堂", "甘味処うさぎ",
]


def make_demo_doujin_items(count: int = 20) -> list[dict]:
    """同人誌APIキーなしでの動作確認用ダミーデータ"""
    rng = random.Random(f"doujin{datetime.now().strftime('%Y%m%d')}")
    items = []
    for i in range(count):
        base_price = rng.choice([550, 770, 980, 1320, 1650])
        on_sale    = rng.random() < 0.3
        price      = int(base_price * rng.uniform(0.5, 0.7)) if on_sale else base_price
        pid        = f"demo_doujin_{i+1:03d}"
        days_ago   = rng.randint(0, 60)
        release_date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")

        items.append({
            "product_id":       pid,
            "title":            rng.choice(_DEMO_DOUJIN_TITLES),
            "actress":          rng.choice(_DEMO_CIRCLES),
            "genres":           rng.choice(["人妻,寝取られ", "学園,催眠", "ファンタジー,ふたなり"]),
            "maker":            rng.choice(_DEMO_CIRCLES),
            "label":            "",
            "release_date":     release_date,
            "minutes":          0,
            "affiliate_url":    f"https://www.dmm.co.jp/demo/{pid}/?lurl=affiliate",
            "product_url":      f"https://www.dmm.co.jp/digital/doujin/-/detail/=/cid={pid}/",
            "thumbnail_url":    f"https://example.com/demo_doujin/{pid}.jpg",
            "sample_movie_url": "",
            "sample_image_urls": [f"https://example.com/demo_doujin/{pid}_sample1.jpg"],
            "price":            price,
            "list_price":       base_price,
        })
    return items


def _scrape_actress_page(actress_name: str, limit: int = 20) -> list[dict]:
    """スクレイプモード用: FANZA女優検索ページから作品を取得"""
    if not HAS_REQUESTS:
        return []
    try:
        encoded = urllib.parse.quote(actress_name)
        url = f"https://www.dmm.co.jp/digital/videoa/-/list/=/keyword={encoded}/"
        resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for rank, item in enumerate(soup.select("li.tmb")[:limit], 1):
            title_el = item.select_one(".tmb-title, .title, a[title]")
            link_el  = item.select_one("a[href]")
            title    = title_el.get_text(strip=True) if title_el else "不明"
            href     = link_el.get("href", "") if link_el else ""
            pid      = f"sc_actress_{rank:04d}_{int(time.time())}"
            results.append({
                "product_id":      pid,
                "title":           title,
                "actress":         actress_name,
                "genres":          "",
                "maker":           "",
                "label":           "",
                "release_date":    "",
                "minutes":         0,
                "affiliate_url":   href,
                "product_url":     href,
                "thumbnail_url":   "",
                "sample_movie_url": "",  # スクレイプでは取得困難
                "price":           0,
                "list_price":      0,
            })
        time.sleep(random.uniform(2.0, 4.0))
        return results
    except Exception as e:
        log.error("女優ページスクレイプ失敗 [%s]: %s", actress_name, e)
        return []


# ────────────────────────────────────────────
# 商品スコアリング
# ────────────────────────────────────────────
def score_product(item: dict, genre_priority: int = 5, actress_avg_likes: float = 0) -> float:
    """
    商品スコアを計算する（高いほど優先投稿）

    要素:
      - セール割引率（最重要：購買転換率が高い）
      - 新作度（発売から日数が浅いほど高い）
      - 女優人気（過去投稿の平均Likes）
      - ジャンル優先度設定
    """
    score = 0.0
    cfg   = SCORING

    # セール割引ボーナス
    list_price = item.get("list_price", 0)
    price      = item.get("price", 0)
    if list_price and price and price < list_price:
        discount_pct = (list_price - price) / list_price * 100
        if discount_pct >= 50:
            score += cfg["discount_50pct_bonus"]
        elif discount_pct >= 30:
            score += cfg["discount_30pct_bonus"]
        elif discount_pct >= 20:
            score += cfg["discount_20pct_bonus"]
        item["discount_pct"] = round(discount_pct, 1)
        item["is_sale"]      = discount_pct >= 20
    else:
        item["discount_pct"] = 0
        item["is_sale"]      = False

    # 新作ボーナス
    release_date = item.get("release_date", "")
    if release_date:
        try:
            rd   = datetime.strptime(release_date[:10], "%Y-%m-%d")
            days = (datetime.now() - rd).days
            if days <= 7:
                score += cfg["new_7days_bonus"]
            elif days <= 30:
                score += cfg["new_30days_bonus"]
        except ValueError:
            pass

    # 女優人気ボーナス（過去エンゲージメント）
    score += min(actress_avg_likes, 30)

    # ジャンル優先度ボーナス（優先度1=+15, 5=+3）
    score += max(0, 18 - genre_priority * 3)

    return score


# ────────────────────────────────────────────
# クライアントファサード
# ────────────────────────────────────────────
class FanzaClient:
    def __init__(self, demo: bool = False):
        self.demo = demo
        api_id       = FANZA["api_id"]
        affiliate_id = FANZA["affiliate_id"]

        if not demo and api_id and api_id != "your_fanza_api_id":
            self._client = FanzaAPIClient(api_id, affiliate_id)
            self._mode   = "api"
        elif not demo and HAS_REQUESTS:
            self._client = FanzaScraper()
            self._mode   = "scrape"
        else:
            self._client = None
            self._mode   = "demo"
            self.demo    = True

    def get_scored_products(
        self,
        genre_key: str,
        genre_priority: int = 5,
        actress_stats: dict = None,
        conn=None,
        require_sample_movie: bool = True,
        genre_id=None,
    ) -> list[dict]:
        """
        スコアリング済み商品リストを返す（高スコア順）。

        Args:
            require_sample_movie: Trueの場合、サンプル動画ありの商品のみ返す
            genre_id: FANZA APIの実際の数値ジャンルID（config.pyのtarget_genresを参照）
        """
        if self.demo:
            items = make_demo_items(genre_key)
        elif self._mode == "api":
            items = self._client.get_items(genre_key, hits=20, genre_id=genre_id)
        else:
            items = self._client.get_items(genre_key, limit=20)

        results = []
        for item in items:
            # サンプル動画フィルター
            if require_sample_movie and not item.get("sample_movie_url"):
                log.debug("  サンプル動画なしのためスキップ: %s", item.get("title", "")[:20])
                continue

            # クールダウンチェック
            if conn:
                from core.database import was_recently_posted
                if was_recently_posted(conn, item["product_id"]):
                    continue

            actress   = (item.get("actress") or "").split(",")[0]
            avg_likes = 0
            if actress_stats and actress in actress_stats:
                avg_likes = actress_stats[actress].get("avg_likes", 0)

            item["score"] = score_product(item, genre_priority, avg_likes)
            results.append(item)

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def get_sale_products(self, genre_key: str, genre_id=None) -> list[dict]:
        """セール中 かつ サンプル動画あり の商品のみ返す"""
        items = self.get_scored_products(genre_key, require_sample_movie=True, genre_id=genre_id)
        return [i for i in items if i.get("is_sale")]

    def get_actress_spotlight(self, actress_name: str) -> list[dict]:
        """週1スポットライト用（後方互換のため残す）"""
        return self.get_actress_products(actress_name, hits=3)

    def get_actress_products(
        self,
        actress_name: str,
        hits: int = 20,
        conn=None,
    ) -> list[dict]:
        """
        指定女優の出演作品をスコアリング済みで返す。
        サンプル動画あり & クールダウン外 の商品のみ。

        デモ/スクレイプモードでは actress_name でデモデータを生成。
        """
        if self.demo:
            items = _make_actress_demo_items(actress_name)
        elif self._mode == "api":
            items = self._client.get_actress_products(actress_name, hits=hits)
        else:
            # スクレイプモードはFANZA女優ページをフォールバック
            items = _scrape_actress_page(actress_name, limit=hits)

        results = []
        for item in items:
            if conn:
                from core.database import was_recently_posted
                if was_recently_posted(conn, item["product_id"]):
                    continue
            item["score"] = score_product(item, genre_priority=3)
            results.append(item)

        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def get_scored_doujin_products(
        self,
        conn=None,
        require_sample_image: bool = True,
    ) -> list[dict]:
        """
        FANZA同人誌ランキングをスコアリング済みで返す（高スコア順）。
        API未設定時はデモデータ、スクレイピングモードは同人誌取得に未対応（空リスト）。
        """
        if self.demo:
            items = make_demo_doujin_items(DOUJIN.get("fetch_hits", 20))
        elif self._mode == "api":
            items = self._client.get_doujin_items(hits=DOUJIN.get("fetch_hits", 20))
        else:
            log.warning("[同人誌] スクレイピングモードは同人誌取得に未対応です")
            items = []

        results = []
        for item in items:
            if require_sample_image and not item.get("thumbnail_url"):
                continue

            if conn:
                from core.database import was_recently_posted
                if was_recently_posted(conn, item["product_id"]):
                    continue

            item["score"] = score_product(item, genre_priority=DOUJIN.get("priority", 4))
            results.append(item)

        results.sort(key=lambda x: x["score"], reverse=True)
        return results
