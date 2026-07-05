"""
main.py — FANZA × Gemini × X 自動投稿システム メインスケジューラー

【投稿フロー】
  1. FANZAから商品取得（APIまたはスクレイピング）
  2. スコアリングして投稿優先順位を決定
  3. Geminiで投稿文（メイン + リプライ）を生成
  4. data/videos/ から自前動画を選択して X にアップロード
  5. メインツイート（動画付き・リンクなし）を投稿
  6. リプライ（アフィリエイトリンク）を投稿
  7. DBに記録 → エンゲージメント追跡でスコアを改善

【動画管理】
  data/videos/           … どのジャンルにも使える共通動画
  data/videos/hitoduma/  … 人妻ジャンル専用動画
  data/videos/ntr/       … NTRジャンル専用動画
  ジャンル別フォルダが優先、なければ共通フォルダからランダム選択

【改善ポイント】
  - メインツイートにリンクなし: Xアルゴリズムによるリーチ抑制を回避
  - 動画付き投稿: 静止画より高エンゲージメント
  - Gemini AI: テンプレートより自然な文章、毎回違う表現
  - セール即時投稿: セール検知後すぐにキュー投入
  - 女優スポットライト: 週1回、女優ファン向け特集投稿
  - ABテスト: 2バリアント生成してどちらが効くか追跡
  - ハッシュタグ最適化: 実績データで自動最適化
"""
import sys
import time
import logging
import random
import argparse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import FANZA, DOUJIN, POST_SCHEDULE, LOG_DIR
from core.database import (
    init_db, get_conn, upsert_product, record_price,
    enqueue, dequeue_next, mark_posted, get_recent_post_count,
    get_sale_products, was_recently_posted,
    update_actress_stats, update_hashtag_stats,
)
from core.fanza_client import FanzaClient
from core.video_manager import pick_video, initialize as init_videos, show_library
from ai.gemini_writer import GeminiWriter
from poster.x_poster import post_item

try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False


# ─────────────────────────────────────────
# ロギング設定
# ─────────────────────────────────────────
def setup_logging(level=logging.INFO):
    log_file = LOG_DIR / f"bot_{datetime.now().strftime('%Y%m')}.log"
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


log = logging.getLogger(__name__)


# ─────────────────────────────────────────
# タスク: FANZA商品取得 & キュー補充
# ─────────────────────────────────────────
def task_fetch_and_queue(demo: bool = False):
    """FANZAから商品を取得してpost_queueに追加する"""
    log.info("=== [タスク] 商品取得 & キュー補充 ===")
    client = FanzaClient(demo=demo)
    writer = GeminiWriter()
    conn   = get_conn()

    # 女優別エンゲージメント実績を取得（スコアリングに使用）
    actress_stats_rows = conn.execute(
        "SELECT actress_name, avg_likes FROM actress_stats"
    ).fetchall()
    actress_stats = {r["actress_name"]: {"avg_likes": r["avg_likes"]}
                     for r in actress_stats_rows}

    queued_total = 0

    for genre_cfg in FANZA["target_genres"]:
        genre_key   = genre_cfg["key"]
        genre_label = genre_cfg["label"]
        priority_no = genre_cfg["priority"]

        log.info("[取得] %s (%s)", genre_label, genre_key)

        products = client.get_scored_products(
            genre_key, priority_no, actress_stats, conn
        )

        for rank, product in enumerate(products[:5], start=1):
            pid       = product["product_id"]
            is_sale   = product.get("is_sale", False)
            post_type = "sale" if is_sale else ("newrelease" if rank == 1 else "ranking")
            priority  = 1 if is_sale else (3 if rank == 1 else 5)

            # 商品マスタに保存
            upsert_product(conn, product)

            # 価格記録
            record_price(
                conn, pid,
                product.get("price", 0),
                product.get("list_price", 0),
                FANZA["sale_threshold_pct"],
            )

            # 再投稿クールダウンチェック
            if was_recently_posted(conn, pid):
                log.debug("  クールダウン中: %s", pid)
                continue

            # Geminiで投稿文生成
            main_body, reply_body = writer.generate_pair(
                product, post_type,
                genre_key=genre_key,
                rank=rank,
                genre_label=genre_label,
                conn=conn,
            )

            if not main_body:
                log.warning("  投稿文生成失敗: %s", pid)
                continue

            qid = enqueue(
                conn,
                post_type  = post_type,
                body       = main_body,
                reply_body = reply_body,
                product_id = pid,
                priority   = priority,
            )

            if qid > 0:
                queued_total += 1
                log.info("  ✅ キュー追加 [%s] rank=%d %s", post_type, rank, product["title"][:20])
            else:
                log.debug("  ⏭ 重複スキップ: %s", pid)

        conn.commit()

    conn.close()
    log.info("=== キュー補充完了: %d件追加 ===", queued_total)
    return queued_total


# ─────────────────────────────────────────
# タスク: 女優特化商品取得 & キュー補充
# ─────────────────────────────────────────
def task_fetch_actress_products(demo: bool = False):
    """
    config.py の FANZA["target_actresses"] に登録した女優の
    出演作品（サンプル動画あり）を取得してキューに追加する。

    ジャンル投稿とは独立して動作し、女優ファン層へのリーチを狙う。
    女優を追加するには config.py の target_actresses リストに名前を追記するだけ。
    """
    target_actresses = FANZA.get("target_actresses", [])
    if not target_actresses:
        return 0

    log.info("=== [タスク] 女優特化商品取得 (%d人) ===", len(target_actresses))
    client = FanzaClient(demo=demo)
    writer = GeminiWriter()
    conn   = get_conn()

    queued_total = 0

    # 女優エンゲージメント実績を取得
    actress_stats_rows = conn.execute(
        "SELECT actress_name, avg_likes FROM actress_stats"
    ).fetchall()
    actress_stats = {r["actress_name"]: {"avg_likes": r["avg_likes"]}
                     for r in actress_stats_rows}

    for actress_cfg in sorted(target_actresses, key=lambda x: x.get("priority", 9)):
        actress_name = actress_cfg["name"]
        log.info("[女優] %s の作品を取得", actress_name)

        products = client.get_actress_products(actress_name, hits=10, conn=conn)

        if not products:
            log.warning("  [女優] 作品が見つかりませんでした: %s", actress_name)
            continue

        # 女優ページ実績をスコアに反映
        avg_likes = actress_stats.get(actress_name, {}).get("avg_likes", 0)
        for p in products:
            p["score"] = p.get("score", 0) + min(avg_likes, 30)

        products.sort(key=lambda x: x["score"], reverse=True)

        for rank, product in enumerate(products[:3], start=1):
            pid       = product["product_id"]
            is_sale   = product.get("is_sale", False)
            post_type = "sale" if is_sale else "actress_spotlight"
            priority  = 1 if is_sale else 2  # 女優特化は通常ジャンルより優先

            upsert_product(conn, product)
            record_price(
                conn, pid,
                product.get("price", 0),
                product.get("list_price", 0),
                FANZA["sale_threshold_pct"],
            )

            main_body, reply_body = writer.generate_pair(
                product,
                post_type,
                genre_key=product.get("genres", "").split(",")[0],
                rank=rank,
                genre_label=actress_name,
                conn=conn,
            )

            if not main_body:
                continue

            qid = enqueue(
                conn,
                post_type  = post_type,
                body       = main_body,
                reply_body = reply_body,
                product_id = pid,
                priority   = priority,
            )

            if qid > 0:
                queued_total += 1
                log.info("  ✅ [%s] %s → キュー追加", actress_name, product["title"][:20])
            else:
                log.debug("  ⏭ 重複スキップ: %s", pid)

        conn.commit()

    conn.close()
    log.info("=== 女優特化キュー補充完了: %d件 ===", queued_total)
    return queued_total


# ─────────────────────────────────────────
# タスク: 同人誌取得 & キュー補充
# ─────────────────────────────────────────
def task_fetch_doujin(demo: bool = False):
    """
    FANZA同人誌ランキングから人気作品を取得してキューに追加する。

    ジャンル投稿（動画）とは独立して動作し、
    メイン投稿にはサンプル画像/表紙画像を添付、リプライにアフィリエイトリンクを付ける。
    """
    if not DOUJIN.get("enabled", True):
        return 0

    log.info("=== [タスク] 同人誌取得 & キュー補充 ===")
    client = FanzaClient(demo=demo)
    writer = GeminiWriter()
    conn   = get_conn()

    products = client.get_scored_doujin_products(conn=conn)

    queued_total = 0
    for rank, product in enumerate(products[:5], start=1):
        pid     = product["product_id"]
        is_sale = product.get("is_sale", False)
        post_type = "sale" if is_sale else "doujin"
        priority  = 1 if is_sale else DOUJIN.get("priority", 4)

        upsert_product(conn, product)
        record_price(
            conn, pid,
            product.get("price", 0),
            product.get("list_price", 0),
            FANZA["sale_threshold_pct"],
        )

        main_body, reply_body = writer.generate_pair(
            product, post_type,
            genre_key="doujin",
            rank=rank,
            genre_label="同人誌",
            conn=conn,
        )

        if not main_body:
            log.warning("  投稿文生成失敗: %s", pid)
            continue

        qid = enqueue(
            conn,
            post_type  = post_type,
            body       = main_body,
            reply_body = reply_body,
            product_id = pid,
            priority   = priority,
        )

        if qid > 0:
            queued_total += 1
            log.info("  ✅ [同人誌] rank=%d %s", rank, product["title"][:20])
        else:
            log.debug("  ⏭ 重複スキップ: %s", pid)

        conn.commit()

    conn.close()
    log.info("=== 同人誌キュー補充完了: %d件 ===", queued_total)
    return queued_total


# ─────────────────────────────────────────
# タスク: セール即時アラート（高頻度チェック）
# ─────────────────────────────────────────
def task_sale_alert(demo: bool = False):
    """
    セール中の商品をチェックし、未通知のものを即時キュー投入。
    1時間ごとに実行し、セール検知時は最優先（priority=1）で投稿。
    """
    log.info("=== [タスク] セールアラートチェック ===")
    client = FanzaClient(demo=demo)
    writer = GeminiWriter()
    conn   = get_conn()

    sale_queued = 0

    for genre_cfg in FANZA["target_genres"]:
        sale_products = client.get_sale_products(genre_cfg["key"])

        for product in sale_products[:2]:
            pid = product["product_id"]

            if was_recently_posted(conn, pid):
                continue

            upsert_product(conn, product)
            record_price(conn, pid, product.get("price", 0),
                         product.get("list_price", 0),
                         FANZA["sale_threshold_pct"])

            main_body, reply_body = writer.generate_pair(
                product, "sale", genre_key=genre_cfg["key"]
            )

            qid = enqueue(conn, "sale", main_body, reply_body, pid, priority=1)
            if qid > 0:
                sale_queued += 1
                log.info("  🔥 セール即時キュー: %s (%.0f%%OFF)",
                         product["title"][:20], product.get("discount_pct", 0))

                # sale_notified に記録（12時間は重複通知しない）
                conn.execute("""
                    INSERT OR IGNORE INTO sale_notified (product_id, notified_at, expires_at)
                    VALUES (?, datetime('now','localtime'), datetime('now','+12 hours','localtime'))
                """, (pid,))

        conn.commit()

    conn.close()
    if sale_queued:
        log.info("=== セールアラート: %d件キュー投入 ===", sale_queued)
    return sale_queued


# ─────────────────────────────────────────
# タスク: 女優スポットライト（週1）
# ─────────────────────────────────────────
def task_actress_spotlight(demo: bool = False):
    """
    エンゲージメント実績が高い女優の特集投稿を作成。
    女優ファン層へのリーチを狙い、フォロワー獲得にも貢献。
    """
    log.info("=== [タスク] 女優スポットライト ===")
    conn = get_conn()

    # 最もLikesが高い女優を選択
    top_actress = conn.execute("""
        SELECT actress_name, avg_likes
        FROM actress_stats
        WHERE total_posts >= 3
          AND last_posted_at < datetime('now', '-6 days', 'localtime')
        ORDER BY avg_likes DESC
        LIMIT 1
    """).fetchone()

    if not top_actress:
        log.info("  スポットライト対象女優なし（実績データ不足）")
        conn.close()
        return

    actress_name = top_actress["actress_name"]
    log.info("  スポットライト女優: %s (avg_likes=%.1f)", actress_name, top_actress["avg_likes"])

    client = FanzaClient(demo=demo)
    products = client.get_actress_spotlight(actress_name)

    if not products:
        # デモ/スクレイプモードは検索できないのでスキップ
        conn.close()
        return

    product = products[0]
    writer  = GeminiWriter()
    main_body, reply_body = writer.generate_pair(
        product, "actress_spotlight",
        genre_key=product.get("genres", "").split(",")[0],
    )

    qid = enqueue(conn, "actress_spotlight", main_body, reply_body,
                  product["product_id"], priority=4)
    conn.commit()
    conn.close()

    if qid > 0:
        log.info("  ✅ スポットライト投稿追加: %s", actress_name)


# ─────────────────────────────────────────
# タスク: 投稿実行
# ─────────────────────────────────────────
def task_post(force: bool = False):
    """
    キューから次の投稿を取り出して実行する。

    戦略:
      - メインツイート: 動画付き・リンクなし（アルゴリズム最適化）
      - リプライ: アフィリエイトリンク（UTMパラメータ付き）
      - 投稿間隔チェック & 日次上限チェック
    """
    conn = get_conn()

    if not force:
        # 時間帯チェック
        now  = datetime.now()
        hour = now.hour
        if hour not in POST_SCHEDULE["post_hours"]:
            conn.close()
            return None

        # 日次上限チェック
        today_count = get_recent_post_count(conn, hours=24)
        if today_count >= POST_SCHEDULE["daily_max"]:
            log.warning("[制限] 本日の投稿上限 (%d/%d)",
                        today_count, POST_SCHEDULE["daily_max"])
            conn.close()
            return None

    # キューから取得
    item_row = dequeue_next(conn)
    if not item_row:
        log.debug("[キュー] 投稿待ちなし")
        conn.close()
        return None

    item_data = dict(item_row)

    # 商品情報取得
    product_row = conn.execute(
        "SELECT * FROM products WHERE product_id = ?",
        (item_data.get("product_id", ""),)
    ).fetchone()
    product = dict(product_row) if product_row else {}

    # メディア選択:
    #   サンプル動画あり(FANZA動画作品) → 自前動画ライブラリから選択
    #   サンプル動画なし(同人誌など) → サンプル画像（最大4枚）、なければサムネイルを添付
    video_path = None
    image_urls = []
    if product.get("sample_movie_url"):
        genre_key  = (product.get("genres") or "").split(",")[0].strip()
        video_path = pick_video(genre_key=genre_key, conn=conn)
        if not video_path:
            log.warning("[動画] 動画なし。data/videos/ に MP4 を配置してください。動画なしで投稿します。")
    else:
        sample_str = product.get("sample_image_urls") or ""
        image_urls = [u for u in sample_str.split(",") if u][:4]
        if not image_urls and product.get("thumbnail_url"):
            image_urls = [product["thumbnail_url"]]

    try:
        result = post_item(
            item       = product,
            main_body  = item_data["body"],
            reply_body = item_data.get("reply_body", ""),
            video_path = video_path,
            image_urls = image_urls,
            variant_id = item_data.get("variant_id", "A"),
        )

        # DB記録
        log_id = mark_posted(
            conn,
            queue_id       = item_data["id"],
            tweet_id       = result["tweet_id"],
            reply_tweet_id = result["reply_tweet_id"],
            body           = item_data["body"],
            product_id     = item_data.get("product_id", ""),
            post_type      = item_data["post_type"],
            variant_id     = result["variant_id"],
            has_image      = result["has_video"] or result["has_image"],
            video_path     = result["video_path"],
        )

        log.info("[投稿完了] type=%s tweet_id=%s video=%s image=%s",
                 item_data["post_type"], result["tweet_id"], result["has_video"], result["has_image"])

        conn.close()
        return result

    except Exception as e:
        log.error("[投稿失敗] %s", e)
        conn.execute(
            "UPDATE post_queue SET status='error' WHERE id=?", (item_data["id"],)
        )
        conn.commit()
        conn.close()
        return None


# ─────────────────────────────────────────
# タスク: エンゲージメント収集 & 統計更新
# ─────────────────────────────────────────
def task_collect_metrics():
    """
    直近48時間の投稿のエンゲージメントを取得し、
    actress_stats / hashtag_stats を更新する。
    → 次の投稿文生成・商品選定に活用
    """
    log.info("=== [タスク] エンゲージメント収集 ===")
    from poster.x_poster import XPoster
    conn   = get_conn()
    poster = XPoster()

    rows = conn.execute("""
        SELECT pl.id, pl.tweet_id, pl.product_id, pl.hashtags,
               p.actress
        FROM post_log pl
        LEFT JOIN products p ON p.product_id = pl.product_id
        WHERE pl.posted_at >= datetime('now', '-48 hours', 'localtime')
          AND pl.tweet_id NOT LIKE 'dry_%'
          AND pl.impressions = 0
    """).fetchall()

    updated = 0
    for row in rows:
        try:
            metrics = poster.get_metrics(row["tweet_id"])
            likes       = metrics.get("like_count", 0)
            retweets    = metrics.get("retweet_count", 0)
            impressions = metrics.get("impression_count", 0)

            conn.execute("""
                UPDATE post_log
                SET likes=?, retweets=?, impressions=?
                WHERE id=?
            """, (likes, retweets, impressions, row["id"]))

            # 女優統計更新
            actress = row["actress"] or ""
            for a in actress.split(","):
                a = a.strip()
                if a:
                    hour = datetime.now().hour
                    update_actress_stats(conn, a, likes, impressions, 0, hour)

            # ハッシュタグ統計更新
            for tag in (row["hashtags"] or "").split():
                if tag.startswith("#"):
                    update_hashtag_stats(conn, tag, likes, impressions)

            updated += 1
            time.sleep(0.5)

        except Exception as e:
            log.warning("  メトリクス取得失敗 tweet_id=%s: %s", row["tweet_id"], e)

    conn.commit()
    conn.close()
    log.info("=== エンゲージメント収集完了: %d件更新 ===", updated)


# ─────────────────────────────────────────
# スケジューラー本体
# ─────────────────────────────────────────
def run_scheduler(demo: bool = False):
    if not HAS_SCHEDULE:
        log.error("schedule ライブラリが必要です: pip install schedule")
        return

    log.info("=" * 55)
    log.info("FANZA × Gemini × X 自動投稿システム 起動")
    log.info("デモモード: %s", demo)
    log.info("=" * 55)

    # 起動時に即実行
    task_fetch_and_queue(demo)
    task_fetch_actress_products(demo)
    task_fetch_doujin(demo)

    # スケジュール設定
    schedule.every(6).hours.do(task_fetch_and_queue, demo=demo)
    schedule.every(8).hours.do(task_fetch_actress_products, demo=demo)
    schedule.every(12).hours.do(task_fetch_doujin, demo=demo)
    schedule.every(1).hours.do(task_sale_alert, demo=demo)

    # 月曜の朝に女優スポットライト生成
    schedule.every().monday.at("06:00").do(task_actress_spotlight, demo=demo)

    # 投稿時間帯ごとに実行（ゆらぎ付き）
    for hour in POST_SCHEDULE["post_hours"]:
        jitter = random.randint(0, POST_SCHEDULE["time_jitter_minutes"])
        sched_time = f"{hour:02d}:{jitter:02d}"
        schedule.every().day.at(sched_time).do(task_post)
        log.info("  投稿スケジュール: %s", sched_time)

    # 深夜3時にエンゲージメント収集
    schedule.every().day.at("03:15").do(task_collect_metrics)

    log.info("スケジューラー稼働中。Ctrl+C で停止。")

    while True:
        try:
            schedule.run_pending()
            # キューが溜まっている場合は追加投稿を試みる
            _try_extra_post()
        except KeyboardInterrupt:
            log.info("停止シグナル受信。終了します。")
            break
        except Exception as e:
            log.error("予期せぬエラー: %s", e)
        time.sleep(60)


def _fetch_single_actress(actress_name: str, demo: bool = False):
    """
    コマンドライン引数 --actress で1人を即時指定して取得・キュー追加するユーティリティ。
    config.py の target_actresses に追加しなくても単発テストに使える。
    """
    log.info("=== [単発] 女優特化取得: %s ===", actress_name)
    client = FanzaClient(demo=demo)
    writer = GeminiWriter()
    conn   = get_conn()

    products = client.get_actress_products(actress_name, hits=10, conn=conn)
    if not products:
        log.warning("作品が見つかりませんでした: %s", actress_name)
        conn.close()
        return

    queued = 0
    for rank, product in enumerate(products[:5], start=1):
        pid       = product["product_id"]
        post_type = "sale" if product.get("is_sale") else "actress_spotlight"

        upsert_product(conn, product)
        record_price(conn, pid, product.get("price", 0),
                     product.get("list_price", 0), FANZA["sale_threshold_pct"])

        main_body, reply_body = writer.generate_pair(
            product, post_type,
            genre_key=product.get("genres", "").split(",")[0],
            rank=rank,
            genre_label=actress_name,
            conn=conn,
        )

        qid = enqueue(conn, post_type, main_body, reply_body, pid, priority=2)
        if qid > 0:
            queued += 1
            log.info("  ✅ rank=%d %s", rank, product["title"][:25])

    conn.commit()
    conn.close()
    print(f"女優特化キュー追加: {queued}件 ({actress_name})")


def _try_extra_post():
    """
    セール等で priority=1 のアイテムが入った時、
    スケジュール外でも追加投稿を試みる（日次上限は守る）。
    """
    conn = get_conn()
    urgent = conn.execute("""
        SELECT COUNT(*) FROM post_queue
        WHERE status='pending' AND priority <= 1
    """).fetchone()[0]
    conn.close()

    if urgent > 0:
        # 最終投稿から最低40分経過していれば投稿
        conn = get_conn()
        last_posted = conn.execute("""
            SELECT posted_at FROM post_log
            ORDER BY posted_at DESC LIMIT 1
        """).fetchone()
        conn.close()

        if last_posted:
            last_dt = datetime.fromisoformat(last_posted["posted_at"])
            if (datetime.now() - last_dt).total_seconds() < 40 * 60:
                return

        log.info("[緊急] セール投稿を即時実行")
        task_post(force=True)


# ─────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="FANZA×Gemini×X 自動投稿Bot")
    parser.add_argument("--demo",        action="store_true", help="デモモード（実際のAPI不要）")
    parser.add_argument("--fetch",       action="store_true", help="ジャンル商品取得 & キュー補充のみ実行")
    parser.add_argument("--fetch-actress", action="store_true", help="女優特化商品取得 & キュー補充のみ実行")
    parser.add_argument("--fetch-doujin", action="store_true", help="同人誌取得 & キュー補充のみ実行")
    parser.add_argument("--actress",     type=str, metavar="NAME", help="指定女優1人の作品を即時取得してキュー追加")
    parser.add_argument("--post",        action="store_true", help="キューから1件投稿して終了")
    parser.add_argument("--metrics",     action="store_true", help="エンゲージメント収集のみ実行")
    parser.add_argument("--sale",        action="store_true", help="セールアラートチェックのみ実行")
    parser.add_argument("--force",       action="store_true", help="時間帯チェックをスキップして投稿")
    parser.add_argument("--sync-videos", action="store_true", help="data/videos/ を走査してDB登録・確認")
    parser.add_argument("--verbose",     action="store_true", help="デバッグログを出力")
    args = parser.parse_args()

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    init_db()

    # 起動時に data/videos/ を走査して未登録動画を自動登録
    conn = get_conn()
    init_videos(conn)
    conn.close()

    if getattr(args, "sync_videos", False):
        conn = get_conn()
        stats = init_videos(conn)
        print(f"動画同期完了: 追加={stats['added']} / 登録済み={stats['already_registered']} / 消失={stats['missing']}")
        show_library(conn)
        conn.close()
    elif args.actress:
        # 1人の女優を即時指定して取得
        _fetch_single_actress(args.actress, demo=args.demo)
    elif getattr(args, "fetch_actress", False):
        count = task_fetch_actress_products(demo=args.demo)
        print(f"女優特化キュー補充完了: {count}件")
    elif getattr(args, "fetch_doujin", False):
        count = task_fetch_doujin(demo=args.demo)
        print(f"同人誌キュー補充完了: {count}件")
    elif args.fetch:
        count = task_fetch_and_queue(demo=args.demo)
        print(f"キュー補充完了: {count}件")
    elif args.post:
        result = task_post(force=args.force)
        print("投稿結果:", result)
    elif args.metrics:
        task_collect_metrics()
    elif args.sale:
        count = task_sale_alert(demo=args.demo)
        print(f"セール検知キュー: {count}件")
    else:
        run_scheduler(demo=args.demo)


if __name__ == "__main__":
    main()
