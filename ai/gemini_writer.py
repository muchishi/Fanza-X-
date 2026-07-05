"""
ai/gemini_writer.py — Gemini API による投稿文生成

戦略:
  - メインツイート: リンクなし（Xのアルゴリズムはリンク付きを抑制する）
  - リプライ: アフィリエイトリンク + 詳細
  - ABテスト: 2バリアント生成し、どちらがパフォーマンス高いか追跡
  - 投稿タイプ別プロンプト: sale / ranking / newrelease / actress_spotlight
"""
import sys
import re
import logging
import random
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import GEMINI, HASHTAG_POOL, REPLY_FALLBACK

try:
    from google import genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# メインツイート用システムプロンプト
# ─────────────────────────────────────────
_SYSTEM_PROMPT = """あなたはFANZAアフィリエイトの投稿専門家です。
Xに投稿する日本語のテキストを1つ作成してください。

【絶対ルール】
- URLは含めない（リプライで案内する）
- 直接的な性的表現は使わない（示唆的・婉曲的に留める）
- 日本語120文字以内（ハッシュタグ込み）
- ハッシュタグは2〜3個（{hashtags}の中から選ぶ）
- 末尾は「↓詳細リプへ」「続きはリプライ👇」などで締める
- 本文のみ出力（説明・前置き・鍵括弧なし）
"""

# ─────────────────────────────────────────
# 投稿タイプ別プロンプトテンプレート
# ─────────────────────────────────────────
_PROMPTS = {
    "sale": """以下のFANZA作品がセール中です。購買衝動を高める投稿文を1つ作成してください。

作品タイトル: {title}
出演: {actress}
割引: {discount}%OFF（定価{list_price}円 → {price}円）
ジャンル: {genres}

ポイント:
- セールの緊急性・期間限定感を出す
- お得感を数字で強調する
- 興味を引く1行目にする
""",

    "ranking": """以下のFANZA作品がランキング上位です。社会的証明を活かした投稿文を1つ作成してください。

作品タイトル: {title}
出演: {actress}
{genre_label}ランキング: {rank}位
ジャンル: {genres}

ポイント:
- 「なぜ人気なのか」への興味を引く
- ランクの数字をうまく使う
- 「みんなが見ている」という感覚を出す
""",

    "newrelease": """以下のFANZA新作を紹介する投稿文を1つ作成してください。

作品タイトル: {title}
出演: {actress}
発売日: {release_date}
ジャンル: {genres}

ポイント:
- 新作の鮮度・注目感を出す
- 出演女優のファンに響く表現
- 「見逃したくない」気持ちを煽る
""",

    "actress_spotlight": """以下の女優のFANZA作品を特集する週イチ投稿文を1つ作成してください。

注目女優: {actress}
代表作: {title}
ジャンル: {genres}

ポイント:
- 女優のファンに向けた温度感
- 「今週注目の女優」という特集感
- ファン心理に刺さる表現
""",

    "doujin": """以下のFANZA同人誌作品を紹介する投稿文を1つ作成してください。

作品タイトル: {title}
作者/サークル: {actress}
ジャンル: {genres}

ポイント:
- マンガ・シチュエーションの気になるポイントを匂わせる
- 「続きが気になる」「読みたい」という感情を刺激する
- 動画作品とは違う、同人誌ならではの魅力（設定・展開）を出す
""",
}

# ─────────────────────────────────────────
# リプライ用プロンプト
# ─────────────────────────────────────────
_REPLY_PROMPT = """以下のFANZA作品の購入リンクを案内する短いリプライ文を作成してください。

作品タイトル: {title}
出演: {actress}
価格: {price}円{sale_note}
アフィリエイトURL: {affiliate_url}

要件:
- 3行以内で簡潔に
- URLをそのまま含める
- 「今すぐ確認」「詳細はこちら」などのCTAを入れる
- 絵文字1〜2個
- 本文のみ出力
"""

_REPLY_PROMPT_DOUJIN = """以下のFANZA同人誌作品の購入リンクを案内する短いリプライ文を作成してください。

作品タイトル: {title}
作者/サークル: {actress}
価格: {price}円{sale_note}
アフィリエイトURL: {affiliate_url}

要件:
- 3行以内で簡潔に
- URLをそのまま含める
- 「今すぐ読む」「詳細はこちら」などのCTAを入れる
- 絵文字1〜2個
- 本文のみ出力
"""


# ─────────────────────────────────────────
# Geminiクライアント
# ─────────────────────────────────────────
class GeminiWriter:
    def __init__(self):
        self._client = None
        self._model  = GEMINI["model"]
        if HAS_GEMINI and GEMINI["api_key"]:
            try:
                self._client = genai.Client(api_key=GEMINI["api_key"])
                log.info("[Gemini] クライアント初期化: %s", self._model)
            except Exception as e:
                log.error("[Gemini] 初期化失敗: %s", e)

    def _call(self, prompt: str, system: str = "") -> Optional[str]:
        if not self._client:
            return None
        try:
            full_prompt = f"{system}\n\n{prompt}" if system else prompt
            resp = self._client.models.generate_content(
                model=self._model,
                contents=full_prompt,
            )
            text = resp.text.strip()
            return text
        except Exception as e:
            log.error("[Gemini] 生成失敗: %s", e)
            return None

    def _pick_hashtags(self, genre_key: str, conn=None) -> list[str]:
        """ジャンルに合ったハッシュタグを選択"""
        if conn:
            try:
                from core.database import get_best_hashtags
                tags = get_best_hashtags(conn, genre_key)
                if tags:
                    return tags
            except Exception:
                pass

        genre_tags  = HASHTAG_POOL.get(genre_key, [])
        common_tags = HASHTAG_POOL.get("common", [])
        selected = []
        if genre_tags:
            selected.append(random.choice(genre_tags))
        if len(genre_tags) > 1:
            remaining = [t for t in genre_tags if t != selected[0]]
            if remaining:
                selected.append(random.choice(remaining))
        if common_tags and len(selected) < 3:
            selected.append(common_tags[0])
        return selected[:3]

    def _truncate(self, text: str, limit: int = 130) -> str:
        """文字数超過時に末尾を切り詰め"""
        if len(text) <= limit:
            return text
        # ハッシュタグ行は保持して本文を切る
        lines = text.split("\n")
        tag_lines   = [l for l in lines if l.startswith("#")]
        body_lines  = [l for l in lines if not l.startswith("#")]
        body = "\n".join(body_lines)
        tags = "\n".join(tag_lines)
        budget = limit - len(tags) - 2
        if budget < 20:
            return text[:limit]
        return body[:budget] + "\n" + tags

    def generate_main(
        self,
        item: dict,
        post_type: str,
        genre_key: str = "",
        rank: int = 1,
        genre_label: str = "",
        conn=None,
    ) -> str:
        """メインツイート本文を生成（Gemini失敗時はテンプレートにフォールバック）"""
        hashtags = self._pick_hashtags(genre_key or "common", conn)
        hashtags_str = " ".join(hashtags)

        template = _PROMPTS.get(post_type, _PROMPTS["ranking"])
        discount = item.get("discount_pct", 0)
        sale_note = f"（{int(discount)}%OFF）" if item.get("is_sale") else ""
        default_role = "作者不明" if post_type == "doujin" else "出演者"

        user_prompt = template.format(
            title        = item.get("title", "")[:30],
            actress      = (item.get("actress") or "").split(",")[0] or default_role,
            price        = f"{item.get('price', 0):,}",
            list_price   = f"{item.get('list_price', 0):,}",
            discount     = int(discount),
            genres       = item.get("genres", ""),
            rank         = rank,
            genre_label  = genre_label,
            release_date = item.get("release_date", ""),
        )

        system = _SYSTEM_PROMPT.format(hashtags=hashtags_str)
        body   = self._call(user_prompt, system)

        if not body:
            body = _fallback_main(item, post_type, hashtags, rank, genre_label)

        body = self._truncate(body, 130)
        return body

    def generate_reply(self, item: dict, post_type: str = "") -> str:
        """リプライ（アフィリエイトリンク付き）を生成"""
        affiliate_url = item.get("affiliate_url", "")
        if not affiliate_url:
            return ""

        # UTMパラメータ付与でCVR追跡
        tracked_url = _build_tracked_url(affiliate_url, item.get("product_id", ""))

        discount = item.get("discount_pct", 0)
        sale_note = f"（現在{int(discount)}%OFFセール中！）" if item.get("is_sale") else ""

        template = _REPLY_PROMPT_DOUJIN if post_type == "doujin" else _REPLY_PROMPT
        default_role = "作者不明" if post_type == "doujin" else "出演者"

        prompt = template.format(
            title         = item.get("title", "")[:25],
            actress       = (item.get("actress") or "").split(",")[0] or default_role,
            price         = f"{item.get('price', 0):,}",
            sale_note     = sale_note,
            affiliate_url = tracked_url,
        )

        reply = self._call(prompt)
        if not reply:
            reply = REPLY_FALLBACK.format(affiliate_url=tracked_url)

        # URLが含まれていなければ末尾に追加
        if tracked_url not in reply:
            reply = reply.rstrip() + f"\n{tracked_url}"

        return reply[:280]

    def generate_pair(
        self,
        item: dict,
        post_type: str,
        genre_key: str = "",
        rank: int = 1,
        genre_label: str = "",
        conn=None,
    ) -> tuple[str, str]:
        """(main_body, reply_body) を生成して返す"""
        main  = self.generate_main(item, post_type, genre_key, rank, genre_label, conn)
        reply = self.generate_reply(item, post_type)
        return main, reply

    def generate_ab_variants(
        self,
        item: dict,
        post_type: str,
        genre_key: str = "",
        conn=None,
    ) -> list[tuple[str, str, str]]:
        """
        ABテスト用に2バリアント生成
        Returns: [(variant_id, main, reply), ...]
        """
        results = []
        for vid in ["A", "B"]:
            main, reply = self.generate_pair(item, post_type, genre_key, conn=conn)
            results.append((vid, main, reply))
        return results


# ─────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────
def _build_tracked_url(url: str, product_id: str) -> str:
    """UTMパラメータ付きトラッキングURLを生成"""
    if not url or "dmm.co.jp" not in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}utm_source=x&utm_medium=affiliate&utm_campaign=fanza_bot&utm_content={product_id}"


def _fallback_main(item: dict, post_type: str, hashtags: list, rank: int, genre_label: str) -> str:
    """Gemini APIが使えない場合のテンプレートフォールバック"""
    title    = item.get("title", "作品")[:28]
    default_role = "作者不明" if post_type == "doujin" else "出演者"
    actress  = (item.get("actress") or "").split(",")[0] or default_role
    discount = int(item.get("discount_pct", 0))
    price    = f"{item.get('price', 0):,}"
    tags     = " ".join(hashtags)

    if post_type == "doujin":
        return f"📖 同人誌イチオシ\n「{title}」\n{actress} 作\n詳細はリプへ👇\n{tags}"
    elif post_type == "sale":
        return f"🔥 {discount}%OFFセール！\n「{title}」\n{actress}出演作が{price}円に\n詳細はリプへ👇\n{tags}"
    elif post_type == "ranking":
        return f"📊 {genre_label}ランキング{rank}位\n「{title}」\n{actress}出演\n詳細はリプへ👇\n{tags}"
    elif post_type == "newrelease":
        return f"🆕 新作公開\n{actress}さん最新作！\n「{title}」\nリプライに詳細👇\n{tags}"
    else:
        return f"✨ 今週の注目作品\n「{title}」\n{actress}出演\n詳細はリプへ👇\n{tags}"
