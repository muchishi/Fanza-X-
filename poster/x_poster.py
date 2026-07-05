"""
poster/x_poster.py — X API v2 投稿エンジン（動画対応版）

動画アップロードフロー（X API v1.1 チャンク方式）:
  INIT   → 動画全体サイズを宣言しmedia_idを取得
  APPEND → 5MB以下のチャンクを順番にアップロード
  FINALIZE → アップロード完了を通知
  STATUS → X側での動画エンコード完了をポーリング確認

メインツイート: 動画付き・リンクなし（アルゴリズム最適化）
リプライ      : アフィリエイトリンク
"""
import sys
import time
import hmac
import base64
import hashlib
import json
import logging
import random
import string
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import X_API, POST_SCHEDULE

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

log = logging.getLogger(__name__)

TWEET_V2_URL = "https://api.twitter.com/2/tweets"
MEDIA_V1_URL = "https://upload.twitter.com/1.1/media/upload.json"

# 動画チャンクサイズ（4MB）。X は1チャンク最大5MBまで許容
CHUNK_SIZE = 4 * 1024 * 1024

# エンコード完了を待つ最大秒数
ENCODE_TIMEOUT_SEC = 120


# ────────────────────────────────────────────
# OAuth 1.0a
# ────────────────────────────────────────────
def _nonce() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=32))

def _ts() -> str:
    return str(int(time.time()))

def _penc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")

def _build_auth_header(
    method: str, url: str, params: dict,
    api_key: str, api_secret: str,
    token: str, token_secret: str,
) -> str:
    oauth = {
        "oauth_consumer_key":     api_key,
        "oauth_nonce":            _nonce(),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        _ts(),
        "oauth_token":            token,
        "oauth_version":          "1.0",
    }
    all_params = {**oauth, **params}
    sorted_str = "&".join(
        f"{_penc(k)}={_penc(v)}"
        for k, v in sorted(all_params.items())
    )
    base = "&".join([_penc(method.upper()), _penc(url), _penc(sorted_str)])
    key  = f"{_penc(api_secret)}&{_penc(token_secret)}"
    sig  = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(
        f'{_penc(k)}="{_penc(v)}"' for k, v in sorted(oauth.items())
    )


# ────────────────────────────────────────────
# XPoster クラス
# ────────────────────────────────────────────
class XPoster:
    def __init__(self, cfg: dict = None):
        cfg = cfg or X_API
        self.api_key      = cfg["api_key"]
        self.api_secret   = cfg["api_secret"]
        self.token        = cfg["access_token"]
        self.token_secret = cfg["access_token_secret"]
        self._dry_run     = not all([self.api_key, self.api_secret,
                                     self.token, self.token_secret])
        if self._dry_run:
            log.warning("[XPoster] APIキー未設定 → DRY-RUNモード")

    def _auth_header(self, method: str, url: str, params: dict = None) -> dict:
        auth = _build_auth_header(
            method, url, params or {},
            self.api_key, self.api_secret,
            self.token, self.token_secret,
        )
        return {"Authorization": auth}

    # ── 動画アップロード（チャンク方式）────────
    def upload_video(self, video_path: Path) -> Optional[str]:
        """
        ローカル動画ファイルを X にアップロードして media_id を返す。

        フェーズ:
          1. INIT    — ファイルサイズ・MIMEタイプを宣言
          2. APPEND  — 4MBチャンクを順番に送信
          3. FINALIZE — アップロード完了を通知
          4. STATUS  — エンコード完了をポーリング
        """
        if self._dry_run:
            log.info("[DRY-RUN] 動画アップロードをスキップ: %s", video_path.name)
            return None

        if not HAS_REQUESTS:
            log.error("[動画] requests が未インストールです")
            return None

        if not video_path or not video_path.exists():
            log.warning("[動画] ファイルが存在しません: %s", video_path)
            return None

        video_bytes = video_path.read_bytes()
        total_bytes = len(video_bytes)
        mime_type   = _mime_type(video_path)

        log.info("[動画] アップロード開始: %s (%.1f MB)",
                 video_path.name, total_bytes / 1024 / 1024)

        try:
            # ── フェーズ1: INIT ─────────────
            media_id = self._video_init(total_bytes, mime_type)
            if not media_id:
                return None

            # ── フェーズ2: APPEND ───────────
            ok = self._video_append(media_id, video_bytes)
            if not ok:
                return None

            # ── フェーズ3: FINALIZE ─────────
            ok = self._video_finalize(media_id)
            if not ok:
                return None

            # ── フェーズ4: STATUS ───────────
            ok = self._video_wait_encode(media_id)
            if not ok:
                return None

            log.info("[動画] アップロード完了: media_id=%s", media_id)
            return media_id

        except Exception as e:
            log.error("[動画] アップロード例外: %s", e)
            return None

    def _video_init(self, total_bytes: int, mime_type: str) -> Optional[str]:
        headers = {**self._auth_header("POST", MEDIA_V1_URL),
                   "Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "command":        "INIT",
            "total_bytes":    str(total_bytes),
            "media_type":     mime_type,
            "media_category": "tweet_video",
        }
        resp = requests.post(MEDIA_V1_URL, headers=headers, data=data, timeout=30)
        if resp.status_code != 202:
            log.error("[動画 INIT] 失敗: %d %s", resp.status_code, resp.text[:200])
            return None
        media_id = resp.json().get("media_id_string")
        log.debug("[動画 INIT] media_id=%s", media_id)
        return media_id

    def _video_append(self, media_id: str, video_bytes: bytes) -> bool:
        total  = len(video_bytes)
        offset = 0
        index  = 0
        while offset < total:
            chunk = video_bytes[offset: offset + CHUNK_SIZE]
            headers = self._auth_header("POST", MEDIA_V1_URL)
            resp = requests.post(
                MEDIA_V1_URL,
                headers=headers,
                data={
                    "command":       "APPEND",
                    "media_id":      media_id,
                    "segment_index": str(index),
                },
                files={"media": chunk},
                timeout=60,
            )
            if resp.status_code != 204:
                log.error("[動画 APPEND] チャンク%d 失敗: %d %s",
                          index, resp.status_code, resp.text[:100])
                return False
            log.debug("[動画 APPEND] chunk %d (%.1f MB送信済み)",
                      index, (offset + len(chunk)) / 1024 / 1024)
            offset += CHUNK_SIZE
            index  += 1
        return True

    def _video_finalize(self, media_id: str) -> bool:
        headers = {**self._auth_header("POST", MEDIA_V1_URL),
                   "Content-Type": "application/x-www-form-urlencoded"}
        resp = requests.post(
            MEDIA_V1_URL,
            headers=headers,
            data={"command": "FINALIZE", "media_id": media_id},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            log.error("[動画 FINALIZE] 失敗: %d %s", resp.status_code, resp.text[:200])
            return False
        log.debug("[動画 FINALIZE] 完了")
        return True

    def _video_wait_encode(self, media_id: str) -> bool:
        """X のエンコードが完了するまでポーリング"""
        deadline = time.time() + ENCODE_TIMEOUT_SEC
        while time.time() < deadline:
            headers = self._auth_header("GET", MEDIA_V1_URL,
                                        {"command": "STATUS", "media_id": media_id})
            resp = requests.get(
                MEDIA_V1_URL,
                params={"command": "STATUS", "media_id": media_id},
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                log.error("[動画 STATUS] 失敗: %d", resp.status_code)
                return False

            info  = resp.json().get("processing_info", {})
            state = info.get("state", "")
            log.debug("[動画 STATUS] state=%s", state)

            if state == "succeeded":
                return True
            if state == "failed":
                log.error("[動画 STATUS] エンコード失敗: %s", info.get("error", {}).get("message", ""))
                return False

            wait = info.get("check_after_secs", 5)
            log.info("[動画] エンコード中... %d秒後に再確認", wait)
            time.sleep(wait)

        log.error("[動画 STATUS] タイムアウト (%d秒)", ENCODE_TIMEOUT_SEC)
        return False

    # ── 画像アップロード（シンプルアップロード）──
    def upload_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[str]:
        """
        画像（サンプル画像・表紙など）をXにアップロードしてmedia_idを返す。
        5MB未満の静止画向けシンプルアップロード（動画のチャンク方式とは別経路）。
        """
        if self._dry_run:
            log.info("[DRY-RUN] 画像アップロードをスキップ")
            return None

        if not HAS_REQUESTS:
            log.error("[画像] requests が未インストールです")
            return None

        if not image_bytes:
            log.warning("[画像] 画像データが空です")
            return None

        headers = self._auth_header("POST", MEDIA_V1_URL)
        try:
            resp = requests.post(
                MEDIA_V1_URL,
                headers=headers,
                files={"media": ("image.jpg", image_bytes, mime_type)},
                timeout=30,
            )
            if resp.status_code in (200, 201):
                media_id = resp.json().get("media_id_string")
                log.info("[画像] アップロード完了: media_id=%s", media_id)
                return media_id
            log.error("[画像] アップロード失敗: %d %s", resp.status_code, resp.text[:200])
            return None
        except Exception as e:
            log.error("[画像] アップロード例外: %s", e)
            return None

    # ── ツイート投稿（v2）────────────────────
    def post_tweet(self, text: str, media_id: Optional[str] = None, media_ids: Optional[list] = None) -> dict:
        if self._dry_run:
            fake_id = f"dry_{int(time.time())}"
            log.info("[DRY-RUN] ツイート: %s...", text[:50])
            return {"id": fake_id, "text": text}

        if not HAS_REQUESTS:
            raise RuntimeError("requests が未インストールです")

        ids = media_ids or ([media_id] if media_id else None)

        payload = {"text": text}
        if ids:
            payload["media"] = {"media_ids": ids}

        headers = {**self._auth_header("POST", TWEET_V2_URL),
                   "Content-Type": "application/json"}
        resp = requests.post(TWEET_V2_URL, headers=headers,
                             data=json.dumps(payload), timeout=15)
        if resp.status_code == 201:
            data = resp.json().get("data", {})
            log.info("[X] ツイート成功: id=%s", data.get("id"))
            return data
        else:
            log.error("[X] ツイート失敗: %d %s", resp.status_code, resp.text[:200])
            raise RuntimeError(f"Tweet failed: {resp.status_code}")

    # ── リプライ投稿（v2）────────────────────
    def reply_tweet(self, text: str, reply_to_id: str) -> dict:
        if self._dry_run:
            fake_id = f"dry_reply_{int(time.time())}"
            log.info("[DRY-RUN] リプライ: %s...", text[:50])
            return {"id": fake_id, "text": text}

        if not HAS_REQUESTS:
            raise RuntimeError("requests が未インストールです")

        payload = {
            "text":  text,
            "reply": {"in_reply_to_tweet_id": reply_to_id},
        }
        headers = {**self._auth_header("POST", TWEET_V2_URL),
                   "Content-Type": "application/json"}
        resp = requests.post(TWEET_V2_URL, headers=headers,
                             data=json.dumps(payload), timeout=15)
        if resp.status_code == 201:
            data = resp.json().get("data", {})
            log.info("[X] リプライ成功: id=%s", data.get("id"))
            return data
        else:
            log.error("[X] リプライ失敗: %d %s", resp.status_code, resp.text[:100])
            raise RuntimeError(f"Reply failed: {resp.status_code}")

    # ── エンゲージメント取得（v2）──────────
    def get_metrics(self, tweet_id: str) -> dict:
        if self._dry_run:
            return {
                "impression_count": random.randint(500, 8000),
                "like_count":       random.randint(10, 200),
                "retweet_count":    random.randint(1, 50),
                "reply_count":      random.randint(0, 20),
            }
        if not HAS_REQUESTS:
            return {}

        url = f"https://api.twitter.com/2/tweets/{tweet_id}"
        headers = self._auth_header("GET", url, {"tweet.fields": "public_metrics"})
        try:
            resp = requests.get(url, params={"tweet.fields": "public_metrics"},
                                headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("data", {}).get("public_metrics", {})
        except Exception as e:
            log.warning("[X] メトリクス取得失敗: %s", e)
        return {}


# ────────────────────────────────────────────
# ユーティリティ
# ────────────────────────────────────────────
def _mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".avi": "video/x-msvideo",
    }.get(ext, "video/mp4")


def _image_mime_type(url: str) -> str:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/jpeg")


def download_image(url: str) -> Optional[bytes]:
    """サンプル画像・表紙URLをダウンロードして投稿用バイト列を返す"""
    if not url or not HAS_REQUESTS:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        log.warning("[画像] ダウンロード失敗: %s", e)
        return None


# ────────────────────────────────────────────
# 投稿実行関数（main.py から呼ぶ）
# ────────────────────────────────────────────
def post_item(
    item: dict,
    main_body: str,
    reply_body: str,
    video_path: Optional[Path] = None,
    image_urls: Optional[list] = None,
    variant_id: str = "A",
    poster: Optional["XPoster"] = None,
) -> dict:
    """
    1商品の投稿フロー全体を実行する:
      1. メディアアップロード（動画: X v1.1 チャンク方式 / 画像: シンプルアップロード、最大4枚）
      2. メインツイート投稿（メディア付き・リンクなし）
      3. リプライ投稿（アフィリエイトリンク）

    video_path と image_urls は排他利用（動画作品 vs 同人誌のサンプル画像）。
    両方渡された場合は動画を優先する。X は1ツイート最大4枚まで画像を添付できる。

    Args:
        poster: テスト時に外からXPosterを渡せる（Noneなら本番設定で生成）
    Returns: 結果dict（tweet_id, reply_tweet_id, has_video, has_image）
    """
    poster = poster or XPoster()

    # メディアアップロード
    media_id   = None
    media_ids  = []
    has_video  = False
    has_image  = False

    if video_path:
        media_id = poster.upload_video(video_path)
        has_video = media_id is not None
        if not media_id:
            log.warning("[投稿] 動画アップロード失敗。動画なしで投稿します。")
    elif image_urls:
        if poster._dry_run:
            log.info("[DRY-RUN] 画像アップロードをスキップ: %d枚", len(image_urls))
        else:
            for url in image_urls[:4]:
                image_bytes = download_image(url)
                if not image_bytes:
                    log.warning("[投稿] 画像ダウンロード失敗、スキップ: %s", url)
                    continue
                mid = poster.upload_image(image_bytes, _image_mime_type(url))
                if mid:
                    media_ids.append(mid)
                else:
                    log.warning("[投稿] 画像アップロード失敗、スキップ: %s", url)
            has_image = len(media_ids) > 0
            if not has_image:
                log.warning("[投稿] 画像アップロードが全て失敗。画像なしで投稿します。")

    # メインツイート
    tweet    = poster.post_tweet(main_body, media_id=media_id, media_ids=media_ids or None)
    tweet_id = tweet.get("id", "")

    # リプライ（アフィリエイトリンク）
    reply_tweet_id = ""
    if reply_body and tweet_id:
        time.sleep(2)
        reply          = poster.reply_tweet(reply_body, tweet_id)
        reply_tweet_id = reply.get("id", "")

    return {
        "tweet_id":       tweet_id,
        "reply_tweet_id": reply_tweet_id,
        "has_video":      has_video,
        "has_image":      has_image,
        "video_path":     str(video_path) if video_path else "",
        "variant_id":     variant_id,
    }
