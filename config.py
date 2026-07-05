"""
config.py — 全設定（.envから読み込み）
"""
from pathlib import Path
import os

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
LOG_DIR   = BASE_DIR / "logs"
DB_PATH   = DATA_DIR / "bot.db"

# 動画フォルダ構成:
#   data/videos/          — ジャンル共通動画
#   data/videos/hitoduma/ — ジャンル別動画（フォルダ名はジャンルキーに対応）
VIDEO_DIR = DATA_DIR / "videos"

for _d in [DATA_DIR, LOG_DIR, VIDEO_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────
# X API v2
# ─────────────────────────────────────────
X_API = {
    "api_key":             os.getenv("X_API_KEY", ""),
    "api_secret":          os.getenv("X_API_SECRET", ""),
    "access_token":        os.getenv("X_ACCESS_TOKEN", ""),
    "access_token_secret": os.getenv("X_ACCESS_TOKEN_SECRET", ""),
}

# ─────────────────────────────────────────
# FANZA アフィリエイトAPI
# ─────────────────────────────────────────
FANZA = {
    "api_id":       os.getenv("FANZA_API_ID", ""),
    "affiliate_id": os.getenv("FANZA_AFFILIATE_ID", ""),

    # 投稿対象ジャンル（priorityが低いほど優先）
    "target_genres": [
        {"key": "hitoduma",  "label": "人妻・熟女",   "priority": 1},
        {"key": "ntr",       "label": "NTR・寝取られ", "priority": 2},
        {"key": "amateur",   "label": "素人・ハメ撮り", "priority": 3},
        {"key": "bigtits",   "label": "巨乳",          "priority": 4},
        {"key": "creampie",  "label": "中出し",         "priority": 5},
    ],

    # セール判定：定価からN%以上値下がりでセール扱い
    "sale_threshold_pct": 20,

    # ─── 特化女優リスト ───────────────────────
    # ここに名前を追加するだけで自動投稿対象になる
    # priority: 低い数字ほど優先（ジャンル投稿と同じルール）
    "target_actresses": [
        # {"name": "波多野結衣", "priority": 1},
        # {"name": "三浦恵理子", "priority": 2},
        # {"name": "紗倉まな",   "priority": 3},
    ],
}

# ─────────────────────────────────────────
# FANZA 同人誌（電子書籍）
# ─────────────────────────────────────────
DOUJIN = {
    "enabled": True,

    # 1回の取得件数
    "fetch_hits": 20,

    # 投稿優先度（低いほど優先。ジャンル投稿・女優特化と同じルール）
    "priority": 4,
}

# ─────────────────────────────────────────
# Gemini API
# ─────────────────────────────────────────
GEMINI = {
    "api_key": os.getenv("GEMINI_API_KEY", ""),
    "model":   "gemini-2.0-flash-lite",
    # 生成バリエーション数（ABテスト用）
    "variants": 2,
}

# ─────────────────────────────────────────
# 投稿スケジュール
# ─────────────────────────────────────────
POST_SCHEDULE = {
    # 1日の最大投稿数（凍結リスク管理）
    "daily_max": 8,

    # 投稿時間帯（24時間表記）— エンゲージメント高い時間帯
    "post_hours": [7, 12, 19, 21, 22, 23],

    # 最小投稿間隔（分）
    "min_interval_minutes": 70,

    # 投稿時刻のランダムゆらぎ（分）— スパム判定回避
    "time_jitter_minutes": 12,
}

# ─────────────────────────────────────────
# 商品スコアリング設定
# ─────────────────────────────────────────
SCORING = {
    # セール割引ボーナス
    "discount_50pct_bonus": 50,
    "discount_30pct_bonus": 30,
    "discount_20pct_bonus": 20,

    # 新作ボーナス（発売からの日数）
    "new_7days_bonus":  20,
    "new_30days_bonus": 10,

    # 再投稿クールダウン（日）
    "repost_cooldown_days": 14,

    # クールダウン中はスコアから除外
    "cooldown_penalty": 9999,
}

# ─────────────────────────────────────────
# ハッシュタグプール
# ─────────────────────────────────────────
HASHTAG_POOL = {
    "hitoduma":  ["#人妻", "#熟女", "#奥様", "#熟女系"],
    "ntr":       ["#NTR", "#寝取られ", "#寝取り"],
    "amateur":   ["#素人", "#ハメ撮り", "#個人撮影"],
    "bigtits":   ["#巨乳", "#爆乳", "#おっぱい"],
    "creampie":  ["#中出し", "#生中出し"],
    "doujin":    ["#同人誌", "#エロ同人", "#同人ソフト"],
    "common":    ["#FANZA", "#アダルト動画", "#動画"],
}

# ─────────────────────────────────────────
# リプライ文テンプレート（Gemini失敗時フォールバック）
# ─────────────────────────────────────────
REPLY_FALLBACK = "🔗 作品の詳細・購入はこちら\n{affiliate_url}\n\n#FANZA"
