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
# Selenium（ブラウザ自動投稿・X API不使用）
# ─────────────────────────────────────────
# 注意: XのAutomation Rulesは自動投稿にAPI利用を求めており、
# ブラウザ自動操作での投稿は規約違反・アカウント凍結リスクを伴う。
# 利用は自己責任で。
SELENIUM = {
    # Chromeのプロファイル保存先。初回に手動ログインすればログイン状態を保持できる
    "profile_dir": str(DATA_DIR / "chrome_profile"),

    # 投稿URL特定に使うXのユーザー名（@なし）。.envのX_USERNAMEで設定
    "username": os.getenv("X_USERNAME", ""),

    # True=ヘッドレス（画面非表示）。初回ログイン時はFalse推奨
    "headless": False,

    # 各操作の待機タイムアウト秒数
    "wait_timeout_sec": 20,
}

# ─────────────────────────────────────────
# Telegram（下書きをスマホへ通知送信）
# ─────────────────────────────────────────
# draftモードで作成した下書き(本文・画像・リプライ)をTelegramに送信し、
# スマホのTelegramアプリからコピペでX投稿できるようにする。
TELEGRAM = {
    "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "chat_id":   os.getenv("TELEGRAM_CHAT_ID", ""),
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
    # "auto"  = X APIで自動投稿（有料プラン必須）
    # "draft" = X APIを使わず data/drafts/ に下書き出力（手動投稿・エンゲージメント収集は無効化）
    "mode": "draft",

    # 1日の最大投稿数（凍結リスク管理）
    "daily_max": 12,

    # 通常投稿時刻（24時間表記）。22:00〜9:00は除外時間帯のため指定しない
    "post_hours": [12],

    # 高頻度時間帯: この時間はinterval_minutesおきに投稿する
    "dense_window": {"start_hour": 18, "end_hour": 22, "interval_minutes": 30},

    # 投稿時刻のランダムゆらぎ（分）— スパム判定回避（通常投稿時刻のみに適用）
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
