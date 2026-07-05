"""
core/database.py — SQLite スキーマ定義 & CRUD

既存 Auto_post/database.py より拡張:
  - actress_stats  : 女優別エンゲージメント実績
  - hashtag_stats  : ハッシュタグ別パフォーマンス
  - ab_variants    : ABテスト用バリアント管理
"""
import sqlite3
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DB_PATH, SCORING


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript("""
    -- 商品マスタ
    CREATE TABLE IF NOT EXISTS products (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id       TEXT NOT NULL UNIQUE,
        title            TEXT NOT NULL,
        actress          TEXT,
        genres           TEXT,
        maker            TEXT,
        label            TEXT,
        release_date     TEXT,
        minutes          INTEGER,
        affiliate_url    TEXT,
        product_url      TEXT,              -- FANZA商品ページURL
        thumbnail_url    TEXT,
        sample_movie_url TEXT,              -- サンプル動画URL（あれば）
        has_sample_movie INTEGER DEFAULT 0, -- サンプル動画ありフラグ
        sample_image_urls TEXT,             -- サンプル画像URL（カンマ区切り、同人誌など）
        score            REAL DEFAULT 0,
        created_at       TEXT DEFAULT (datetime('now','localtime')),
        updated_at       TEXT DEFAULT (datetime('now','localtime'))
    );

    -- 価格履歴（セール検知の中核）
    CREATE TABLE IF NOT EXISTS price_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id   TEXT NOT NULL REFERENCES products(product_id),
        price        INTEGER NOT NULL,
        list_price   INTEGER,
        is_sale      INTEGER DEFAULT 0,
        discount_pct REAL DEFAULT 0,
        fetched_at   TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_ph_product ON price_history(product_id);
    CREATE INDEX IF NOT EXISTS idx_ph_sale    ON price_history(is_sale, fetched_at);

    -- ランキング履歴
    CREATE TABLE IF NOT EXISTS rankings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        fetch_date  TEXT NOT NULL,
        genre_key   TEXT NOT NULL,
        genre_label TEXT,
        rank        INTEGER NOT NULL,
        product_id  TEXT NOT NULL REFERENCES products(product_id),
        UNIQUE(fetch_date, genre_key, rank)
    );

    -- 投稿キュー
    CREATE TABLE IF NOT EXISTS post_queue (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        post_type    TEXT NOT NULL,
        product_id   TEXT REFERENCES products(product_id),
        body         TEXT NOT NULL,
        reply_body   TEXT,
        variant_id   TEXT DEFAULT 'A',
        priority     INTEGER DEFAULT 5,
        scheduled_at TEXT,
        status       TEXT DEFAULT 'pending',
        draft_path   TEXT,
        created_at   TEXT DEFAULT (datetime('now','localtime')),
        posted_at    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_pq_status ON post_queue(status, priority, scheduled_at);

    -- 投稿ログ
    CREATE TABLE IF NOT EXISTS post_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        queue_id       INTEGER REFERENCES post_queue(id),
        tweet_id       TEXT,
        reply_tweet_id TEXT,
        product_id     TEXT,
        post_type      TEXT,
        variant_id     TEXT DEFAULT 'A',
        body           TEXT,
        body_hash      TEXT,
        has_image      INTEGER DEFAULT 0,
        video_path     TEXT,
        hashtags       TEXT,
        posted_at      TEXT DEFAULT (datetime('now','localtime')),
        likes          INTEGER DEFAULT 0,
        retweets       INTEGER DEFAULT 0,
        replies        INTEGER DEFAULT 0,
        impressions    INTEGER DEFAULT 0,
        clicks         INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pl_product   ON post_log(product_id);
    CREATE INDEX IF NOT EXISTS idx_pl_hash      ON post_log(body_hash);
    CREATE INDEX IF NOT EXISTS idx_pl_posted_at ON post_log(posted_at);

    -- 女優別エンゲージメント統計（投稿するたびに更新）
    CREATE TABLE IF NOT EXISTS actress_stats (
        actress_name    TEXT PRIMARY KEY,
        total_posts     INTEGER DEFAULT 0,
        total_likes     INTEGER DEFAULT 0,
        total_impressions INTEGER DEFAULT 0,
        total_clicks    INTEGER DEFAULT 0,
        avg_likes       REAL DEFAULT 0,
        avg_impressions REAL DEFAULT 0,
        best_post_hour  INTEGER DEFAULT 22,
        last_posted_at  TEXT,
        updated_at      TEXT DEFAULT (datetime('now','localtime'))
    );

    -- ハッシュタグ別パフォーマンス
    CREATE TABLE IF NOT EXISTS hashtag_stats (
        hashtag         TEXT PRIMARY KEY,
        total_uses      INTEGER DEFAULT 0,
        total_likes     INTEGER DEFAULT 0,
        total_impressions INTEGER DEFAULT 0,
        avg_likes       REAL DEFAULT 0,
        avg_impressions REAL DEFAULT 0,
        last_used_at    TEXT,
        updated_at      TEXT DEFAULT (datetime('now','localtime'))
    );

    -- ABテスト管理
    CREATE TABLE IF NOT EXISTS ab_variants (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id  TEXT,
        post_type   TEXT,
        variant_id  TEXT,
        body        TEXT,
        reply_body  TEXT,
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    );

    -- CVRトラッキング
    CREATE TABLE IF NOT EXISTS cvr_tracking (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        post_log_id INTEGER REFERENCES post_log(id),
        product_id  TEXT,
        param_key   TEXT UNIQUE,
        clicks      INTEGER DEFAULT 0,
        conversions INTEGER DEFAULT 0,
        revenue     INTEGER DEFAULT 0,
        tracked_at  TEXT DEFAULT (datetime('now','localtime'))
    );

    -- セール通知済みキャッシュ（二重投稿防止）
    CREATE TABLE IF NOT EXISTS sale_notified (
        product_id  TEXT NOT NULL,
        notified_at TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        PRIMARY KEY(product_id, notified_at)
    );

    -- 動画ライブラリ（ファイルはディスク、メタデータをDBで管理）
    CREATE TABLE IF NOT EXISTS videos (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        filename     TEXT NOT NULL,
        file_path    TEXT NOT NULL UNIQUE,   -- 絶対パス
        genre_key    TEXT DEFAULT '',        -- '' = ジャンル共通
        file_size_kb INTEGER DEFAULT 0,
        duration_sec INTEGER DEFAULT 0,      -- 動画の長さ（秒）
        active       INTEGER DEFAULT 1,      -- 0 = 使用停止
        used_count   INTEGER DEFAULT 0,      -- 累計使用回数
        total_likes  INTEGER DEFAULT 0,      -- この動画を使った投稿の累計Likes
        avg_likes    REAL DEFAULT 0,         -- 平均Likes（効果測定）
        last_used_at TEXT,
        registered_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_videos_genre  ON videos(genre_key, active);
    CREATE INDEX IF NOT EXISTS idx_videos_active ON videos(active, used_count);

    -- 汎用キーバリューストア（Telegramのlast_update_idなど、単純な状態保存用）
    CREATE TABLE IF NOT EXISTS kv_store (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()
    _migrate(conn)
    conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """既存DBに不足カラムを追加するマイグレーション（冪等）"""
    migrations = [
        "ALTER TABLE products ADD COLUMN product_url      TEXT",
        "ALTER TABLE products ADD COLUMN sample_movie_url TEXT",
        "ALTER TABLE products ADD COLUMN has_sample_movie INTEGER DEFAULT 0",
        "ALTER TABLE products ADD COLUMN sample_image_urls TEXT",
        "ALTER TABLE post_log ADD COLUMN video_path       TEXT",
        "ALTER TABLE post_log ADD COLUMN reply_tweet_id   TEXT",
        "ALTER TABLE post_queue ADD COLUMN draft_path      TEXT",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass  # カラムが既に存在する場合はスキップ
    conn.commit()


# ────────────────────────────────────────────
# 商品操作
# ────────────────────────────────────────────
def upsert_product(conn: sqlite3.Connection, product: dict) -> None:
    sample_image_urls = product.get("sample_image_urls") or []
    conn.execute("""
        INSERT INTO products
            (product_id, title, actress, genres, maker, label,
             release_date, minutes, affiliate_url, product_url,
             thumbnail_url, sample_movie_url, has_sample_movie, sample_image_urls, score, updated_at)
        VALUES
            (:product_id,:title,:actress,:genres,:maker,:label,
             :release_date,:minutes,:affiliate_url,:product_url,
             :thumbnail_url,:sample_movie_url,:has_sample_movie,:sample_image_urls,:score,datetime('now','localtime'))
        ON CONFLICT(product_id) DO UPDATE SET
            title            = excluded.title,
            actress          = excluded.actress,
            genres           = excluded.genres,
            maker            = excluded.maker,
            affiliate_url    = excluded.affiliate_url,
            product_url      = excluded.product_url,
            thumbnail_url    = excluded.thumbnail_url,
            sample_movie_url = excluded.sample_movie_url,
            has_sample_movie = excluded.has_sample_movie,
            sample_image_urls = excluded.sample_image_urls,
            score            = excluded.score,
            updated_at       = excluded.updated_at
    """, {
        **product,
        "score":            product.get("score", 0),
        "product_url":      product.get("product_url", ""),
        "sample_movie_url": product.get("sample_movie_url", ""),
        "has_sample_movie": int(bool(product.get("sample_movie_url", ""))),
        "sample_image_urls": ",".join(sample_image_urls),
    })


def record_price(
    conn: sqlite3.Connection,
    product_id: str,
    price: int,
    list_price: Optional[int],
    threshold: float = 20.0,
) -> dict:
    discount_pct = 0.0
    is_sale = 0
    if list_price and list_price > 0 and price < list_price:
        discount_pct = (list_price - price) / list_price * 100
        is_sale = 1 if discount_pct >= threshold else 0

    conn.execute("""
        INSERT INTO price_history (product_id, price, list_price, is_sale, discount_pct)
        VALUES (?, ?, ?, ?, ?)
    """, (product_id, price, list_price, is_sale, round(discount_pct, 1)))

    return {"is_sale": bool(is_sale), "discount_pct": round(discount_pct, 1)}


def get_sale_products(conn: sqlite3.Connection, limit: int = 20) -> list:
    return conn.execute("""
        SELECT p.*, ph.price, ph.list_price, ph.discount_pct, ph.fetched_at
        FROM products p
        JOIN price_history ph ON p.product_id = ph.product_id
        WHERE ph.is_sale = 1
          AND ph.fetched_at >= datetime('now', '-2 hours', 'localtime')
          AND p.product_id NOT IN (
              SELECT product_id FROM sale_notified
              WHERE expires_at > datetime('now','localtime')
          )
        ORDER BY ph.discount_pct DESC
        LIMIT ?
    """, (limit,)).fetchall()


def was_recently_posted(conn: sqlite3.Connection, product_id: str) -> bool:
    cooldown = SCORING["repost_cooldown_days"]
    row = conn.execute("""
        SELECT 1 FROM post_log
        WHERE product_id = ?
          AND posted_at >= datetime('now',? || ' days','localtime')
    """, (product_id, f"-{cooldown}")).fetchone()
    return row is not None


# ────────────────────────────────────────────
# 投稿キュー操作
# ────────────────────────────────────────────
def enqueue(
    conn: sqlite3.Connection,
    post_type: str,
    body: str,
    reply_body: str = "",
    product_id: str = "",
    priority: int = 5,
    scheduled_at: str = "",
    variant_id: str = "A",
) -> int:
    body_hash = hashlib.md5(body.encode()).hexdigest()

    exists = conn.execute("""
        SELECT 1 FROM post_log
        WHERE body_hash = ?
          AND posted_at >= datetime('now','-14 days','localtime')
    """, (body_hash,)).fetchone()
    if exists:
        return -1

    # 同じ商品がまだ pending/draft でキューに残っている場合は二重登録しない
    # （定期的な商品再取得のたびに、未消化のキューへ何度も追加されるのを防ぐ）
    if product_id:
        already_queued = conn.execute("""
            SELECT 1 FROM post_queue
            WHERE product_id = ? AND status IN ('pending', 'draft')
        """, (product_id,)).fetchone()
        if already_queued:
            return -1

    cur = conn.execute("""
        INSERT INTO post_queue
            (post_type, product_id, body, reply_body, variant_id, priority, scheduled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (post_type, product_id, body, reply_body, variant_id,
          priority, scheduled_at or None))
    return cur.lastrowid


def dequeue_next(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("""
        SELECT * FROM post_queue
        WHERE status = 'pending'
          AND (scheduled_at IS NULL OR scheduled_at <= datetime('now','localtime'))
        ORDER BY priority ASC, created_at ASC
        LIMIT 1
    """).fetchone()


def mark_draft(conn: sqlite3.Connection, queue_id: int, draft_path: str) -> None:
    """
    半自動投稿モード: X APIを呼ばずローカルに下書きを出力した際、
    post_queueのステータスを'draft'にして二重生成を防ぐ。
    実際にユーザーが手動投稿した後は confirm_posted() で 'posted' に遷移させる。
    """
    conn.execute(
        "UPDATE post_queue SET status='draft', draft_path=? WHERE id=?",
        (draft_path, queue_id)
    )
    conn.commit()


def get_draft_queue_item(conn: sqlite3.Connection, queue_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM post_queue WHERE id=? AND status='draft'",
        (queue_id,)
    ).fetchone()


def mark_posted(
    conn: sqlite3.Connection,
    queue_id: int,
    tweet_id: str,
    reply_tweet_id: str,
    body: str,
    product_id: str,
    post_type: str,
    variant_id: str = "A",
    has_image: bool = False,
    hashtags: str = "",
    video_path: str = "",
) -> int:
    body_hash = hashlib.md5(body.encode()).hexdigest()
    conn.execute(
        "UPDATE post_queue SET status='posted', posted_at=datetime('now','localtime') WHERE id=?",
        (queue_id,)
    )
    cur = conn.execute("""
        INSERT INTO post_log
            (queue_id, tweet_id, reply_tweet_id, product_id, post_type,
             variant_id, body, body_hash, has_image, video_path, hashtags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (queue_id, tweet_id, reply_tweet_id, product_id, post_type,
          variant_id, body, body_hash, int(has_image), video_path or None, hashtags))
    conn.commit()
    return cur.lastrowid


def get_recent_post_count(conn: sqlite3.Connection, hours: int = 24) -> int:
    row = conn.execute("""
        SELECT COUNT(*) FROM post_log
        WHERE posted_at >= datetime('now',? || ' hours','localtime')
    """, (f"-{hours}",)).fetchone()
    return row[0]


# ────────────────────────────────────────────
# 女優・ハッシュタグ統計更新
# ────────────────────────────────────────────
def update_actress_stats(
    conn: sqlite3.Connection,
    actress_name: str,
    likes: int,
    impressions: int,
    clicks: int,
    post_hour: int,
) -> None:
    conn.execute("""
        INSERT INTO actress_stats (actress_name, total_posts, total_likes, total_impressions,
                                   total_clicks, avg_likes, avg_impressions, best_post_hour, last_posted_at)
        VALUES (?, 1, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
        ON CONFLICT(actress_name) DO UPDATE SET
            total_posts      = total_posts + 1,
            total_likes      = total_likes + ?,
            total_impressions= total_impressions + ?,
            total_clicks     = total_clicks + ?,
            avg_likes        = CAST(total_likes + ? AS REAL) / (total_posts + 1),
            avg_impressions  = CAST(total_impressions + ? AS REAL) / (total_posts + 1),
            last_posted_at   = datetime('now','localtime'),
            updated_at       = datetime('now','localtime')
    """, (actress_name, likes, impressions, clicks,
          float(likes), float(impressions),
          likes, impressions, clicks, likes, impressions))


def update_hashtag_stats(
    conn: sqlite3.Connection,
    hashtag: str,
    likes: int,
    impressions: int,
) -> None:
    conn.execute("""
        INSERT INTO hashtag_stats (hashtag, total_uses, total_likes, total_impressions,
                                   avg_likes, avg_impressions, last_used_at)
        VALUES (?, 1, ?, ?, ?, ?, datetime('now','localtime'))
        ON CONFLICT(hashtag) DO UPDATE SET
            total_uses       = total_uses + 1,
            total_likes      = total_likes + ?,
            total_impressions= total_impressions + ?,
            avg_likes        = CAST(total_likes + ? AS REAL) / (total_uses + 1),
            avg_impressions  = CAST(total_impressions + ? AS REAL) / (total_uses + 1),
            last_used_at     = datetime('now','localtime'),
            updated_at       = datetime('now','localtime')
    """, (hashtag, likes, impressions, float(likes), float(impressions),
          likes, impressions, likes, impressions))


def get_best_hashtags(conn: sqlite3.Connection, genre_key: str, limit: int = 3) -> list[str]:
    """エンゲージメント実績からジャンルに合う最適ハッシュタグを返す"""
    from config import HASHTAG_POOL
    genre_tags = HASHTAG_POOL.get(genre_key, [])
    common_tags = HASHTAG_POOL.get("common", [])

    rows = conn.execute("""
        SELECT hashtag, avg_impressions FROM hashtag_stats
        WHERE hashtag IN ({placeholders})
        ORDER BY avg_impressions DESC
        LIMIT ?
    """.format(placeholders=",".join("?" * len(genre_tags))),
        genre_tags + [limit]
    ).fetchall()

    best = [r["hashtag"] for r in rows]
    # 実績データが少ない場合はプールから補充
    for tag in genre_tags:
        if tag not in best and len(best) < 2:
            best.append(tag)
    if common_tags and len(best) < limit:
        best.append(common_tags[0])

    return best[:limit]


# ────────────────────────────────────────────
# 動画ライブラリ操作
# ────────────────────────────────────────────
def register_video(
    conn: sqlite3.Connection,
    file_path: str,
    genre_key: str = "",
    duration_sec: int = 0,
) -> int:
    """
    動画ファイルをDBに登録する。
    すでに登録済みの場合はスキップ（file_path がUNIQUE）。

    Args:
        file_path:    動画ファイルの絶対パス
        genre_key:    FANZAジャンルキー。空文字 = ジャンル共通
        duration_sec: 動画の長さ（秒）。0 = 未計測
    Returns:
        登録したID。重複の場合は -1。
    """
    p = Path(file_path)
    if not p.exists():
        return -1

    file_size_kb = p.stat().st_size // 1024

    try:
        cur = conn.execute("""
            INSERT INTO videos (filename, file_path, genre_key, file_size_kb, duration_sec)
            VALUES (?, ?, ?, ?, ?)
        """, (p.name, str(p), genre_key, file_size_kb, duration_sec))
        conn.commit()
        return cur.lastrowid
    except Exception:
        return -1  # UNIQUE制約違反 = 登録済み


def pick_video_from_db(
    conn: sqlite3.Connection,
    genre_key: str = "",
) -> Optional[sqlite3.Row]:
    """
    DBから次に使う動画を選択して返す。

    選択ルール:
      1. 指定ジャンルの動画を優先（used_count が少ない順）
      2. なければジャンル共通（genre_key=''）から選択
      3. 同数なら last_used_at が古い順（均等ローテーション）
    """
    # ジャンル別 → 共通 の順で候補を取得
    for gk in ([genre_key, ""] if genre_key else [""]):
        row = conn.execute("""
            SELECT * FROM videos
            WHERE active = 1
              AND genre_key = ?
            ORDER BY used_count ASC, last_used_at ASC
            LIMIT 1
        """, (gk,)).fetchone()
        if row:
            return row
    return None


def mark_video_used(
    conn: sqlite3.Connection,
    video_id: int,
    likes: int = 0,
) -> None:
    """動画を使用済みとしてDB更新（使用回数・Likes実績を蓄積）"""
    conn.execute("""
        UPDATE videos SET
            used_count   = used_count + 1,
            total_likes  = total_likes + ?,
            avg_likes    = CAST(total_likes + ? AS REAL) / (used_count + 1),
            last_used_at = datetime('now','localtime')
        WHERE id = ?
    """, (likes, likes, video_id))
    conn.commit()


def sync_videos_from_disk(conn: sqlite3.Connection) -> dict:
    """
    VIDEO_DIR を走査して未登録の動画を自動登録する。
    main.py 起動時に呼ぶことでフォルダに動画を置くだけで登録される。

    Returns:
        {"added": N, "already_registered": M, "missing": K}
    """
    from config import VIDEO_DIR

    _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".m4v"}
    stats = {"added": 0, "already_registered": 0, "missing": 0}

    if not VIDEO_DIR.exists():
        return stats

    # ルート直下 = ジャンル共通
    # サブフォルダ名 = ジャンルキー
    scan_targets: list[tuple[Path, str]] = []
    for p in VIDEO_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in _VIDEO_EXTS:
            scan_targets.append((p, ""))
        elif p.is_dir():
            for vp in p.iterdir():
                if vp.is_file() and vp.suffix.lower() in _VIDEO_EXTS:
                    scan_targets.append((vp, p.name))

    for file_path, genre_key in scan_targets:
        result = register_video(conn, str(file_path), genre_key)
        if result > 0:
            stats["added"] += 1
        else:
            stats["already_registered"] += 1

    # DB上に登録されているがファイルが消えているものを非アクティブ化
    all_registered = conn.execute("SELECT id, file_path FROM videos WHERE active=1").fetchall()
    for row in all_registered:
        if not Path(row["file_path"]).exists():
            conn.execute("UPDATE videos SET active=0 WHERE id=?", (row["id"],))
            stats["missing"] += 1
    conn.commit()

    return stats


def list_videos_db(conn: sqlite3.Connection) -> list[dict]:
    """登録済み動画の一覧を返す（管理・確認用）"""
    rows = conn.execute("""
        SELECT id, filename, genre_key, file_size_kb, used_count,
               avg_likes, last_used_at, active
        FROM videos
        ORDER BY genre_key, used_count DESC
    """).fetchall()
    return [dict(r) for r in rows]


# ────────────────────────────────────────────
# 汎用キーバリューストア
# ────────────────────────────────────────────
def get_kv(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("""
        INSERT INTO kv_store (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"[DB] 初期化完了: {DB_PATH}")
