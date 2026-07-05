"""
test_flow.py — GeminiPost システム エンドツーエンドテスト

実行方法:
  cd GeminiPost
  py -3.12 test_flow.py
"""
import sys
import os
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)  # テスト中はWARNING以上のみ表示

sys.path.insert(0, str(Path(__file__).parent))

# ─────────────────────────────────────────────
# テストヘルパー
# ─────────────────────────────────────────────
_pass = 0
_fail = 0

def _safe_print(text: str):
    """Windowsコンソール(cp932)で絵文字が含まれていても落ちないように出力"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))

def ok(label: str):
    global _pass
    _pass += 1
    _safe_print(f"  [OK] {label}")

def ng(label: str, err):
    global _fail
    _fail += 1
    _safe_print(f"  [NG] {label}: {err}")

def section(title: str):
    _safe_print(f"\n{'='*50}")
    _safe_print(f"  {title}")
    _safe_print(f"{'='*50}")

# ─────────────────────────────────────────────
# 1. 設定読み込みテスト
# ─────────────────────────────────────────────
section("1. 設定読み込み")
try:
    from config import X_API, FANZA, GEMINI, POST_SCHEDULE, VIDEO_DIR, DB_PATH
    ok("config.py インポート")
    ok(f"DB_PATH = {DB_PATH}")
    ok(f"VIDEO_DIR = {VIDEO_DIR}")
    # APIキーの設定状況を表示
    x_status = "設定済み" if X_API["api_key"] else "未設定(DRY-RUNモード)"
    g_status  = "設定済み" if GEMINI["api_key"] else "未設定(フォールバックモード)"
    f_status  = "設定済み" if FANZA["api_id"] else "未設定(DEMOモード)"
    print(f"  [INFO] X API: {x_status}")
    print(f"  [INFO] Gemini: {g_status}")
    print(f"  [INFO] FANZA: {f_status}")
except Exception as e:
    ng("config.py インポート", e)

# ─────────────────────────────────────────────
# 2. DB初期化テスト
# ─────────────────────────────────────────────
section("2. データベース初期化")
conn = None
try:
    from core.database import init_db, get_conn
    conn = get_conn()
    ok("SQLite 接続")

    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    required = ["products", "post_log", "videos", "post_queue", "hashtag_stats", "actress_stats"]
    missing  = [t for t in required if t not in tables]
    if missing:
        ng(f"テーブル確認", f"未作成: {missing}")
    else:
        ok(f"全テーブル確認 ({', '.join(required)})")

    # カラム確認
    cur = conn.execute("PRAGMA table_info(products)")
    cols = [r[1] for r in cur.fetchall()]
    for c in ["product_url", "sample_movie_url", "has_sample_movie"]:
        if c in cols:
            ok(f"products.{c} カラム存在")
        else:
            ng(f"products.{c} カラム", "カラムが見つかりません")

    cur = conn.execute("PRAGMA table_info(post_log)")
    cols = [r[1] for r in cur.fetchall()]
    for c in ["video_path", "reply_tweet_id"]:
        if c in cols:
            ok(f"post_log.{c} カラム存在")
        else:
            ng(f"post_log.{c} カラム", "カラムが見つかりません")

except Exception as e:
    ng("DB初期化", e)

# ─────────────────────────────────────────────
# 3. FANZA DEMOデータテスト
# ─────────────────────────────────────────────
section("3. FANZA デモデータ取得")
demo_item = None
try:
    from core.fanza_client import FanzaClient

    client = FanzaClient(demo=True)
    products = client.get_scored_products(
        genre_key="hitoduma",
        genre_priority=1,
        conn=conn,
        require_sample_movie=True,
    )

    ok(f"デモ商品取得: {len(products)}件")
    if products:
        demo_item = products[0]
        # 必須フィールド確認
        for field in ["product_id", "title", "affiliate_url", "has_sample_movie", "sample_movie_url"]:
            val = demo_item.get(field)
            if val:
                ok(f"  {field}: {str(val)[:40]}")
            else:
                ng(f"  フィールド {field}", f"値なし (={val!r})")

        sales = [p for p in products if p.get("is_sale")]
        ok(f"  セール品: {len(sales)}件 / {len(products)}件")
    else:
        ng("デモ商品取得", "0件返却")

except Exception as e:
    ng("FANZA デモ", e)
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────
# 4. キュー操作テスト
# ─────────────────────────────────────────────
section("4. キュー操作（enqueue / dequeue）")
queue_id = None
try:
    from core.database import enqueue, dequeue_next

    if demo_item:
        from core.database import upsert_product
        upsert_product(conn, demo_item)

        _test_body       = f"テスト投稿_{demo_item.get('product_id','x')}"
        _test_reply_body = f"リプライ https://affiliate.dmm.com/test"
        queue_id = enqueue(
            conn,
            post_type  = "ranking",
            body       = _test_body,
            reply_body = _test_reply_body,
            product_id = demo_item.get("product_id", ""),
            priority   = 0,
        )
        if queue_id > 0:
            ok(f"enqueue: queue_id={queue_id}")
        elif queue_id == -1:
            ok("enqueue: 重複スキップ（正常）")
            queue_id = None
        else:
            ng("enqueue", f"想定外の戻り値: {queue_id}")

        if queue_id:
            row = dequeue_next(conn)
            if row:
                ok(f"dequeue_next: id={row['id']}, type={row['post_type']}")
                queue_id = row["id"]
            else:
                ng("dequeue_next", "Noneが返りました")
    else:
        _safe_print("  [SKIP] demo_itemがないためスキップ")

except Exception as e:
    ng("キュー操作", e)
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────
# 5. Gemini フォールバックテスト
# ─────────────────────────────────────────────
section("5. 投稿文生成（Gemini または フォールバック）")
main_body  = ""
reply_body = ""
try:
    from ai.gemini_writer import GeminiWriter

    writer    = GeminiWriter()
    test_item = demo_item if isinstance(demo_item, dict) else {
        "product_id":    "test001",
        "title":         "テスト作品タイトル",
        "actress":       "テスト女優",
        "price":         980,
        "list_price":    1980,
        "discount_pct":  50.5,
        "is_sale":       True,
        "genres":        "人妻",
        "affiliate_url": "https://affiliate.dmm.com/api/test",
        "has_sample_movie": 1,
    }

    main_body, reply_body = writer.generate_pair(
        test_item, "sale", genre_key="hitoduma", rank=1,
        genre_label="人妻・熟女", conn=conn,
    )

    if main_body:
        ok(f"main_body 生成 ({len(main_body)}文字)")
        _safe_print(f"  [本文] {main_body[:80].replace(chr(10), ' ')}...")
    else:
        ng("main_body 生成", "空文字")

    if reply_body:
        ok(f"reply_body 生成 ({len(reply_body)}文字)")
        _safe_print(f"  [リプ] {reply_body[:60].replace(chr(10), ' ')}...")
    else:
        _safe_print("  [INFO] reply_body 空（affiliate_url未設定の場合は正常）")

except Exception as e:
    ng("投稿文生成", e)
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────
# 6. XPoster DRY-RUNテスト
# ─────────────────────────────────────────────
section("6. XPoster DRY-RUN テスト")
try:
    from poster.x_poster import XPoster, post_item

    # 空クレデンシャルで強制DRY-RUN
    dry_cfg = {
        "api_key":             "",
        "api_secret":          "",
        "access_token":        "",
        "access_token_secret": "",
    }
    poster = XPoster(cfg=dry_cfg)

    assert poster._dry_run, "DRY-RUNが有効になっていません"
    ok("XPoster DRY-RUN 初期化")

    tw_result = poster.post_tweet("テストツイート #テスト")
    assert tw_result.get("id", "").startswith("dry_"), f"id={tw_result.get('id')}"
    ok(f"post_tweet: id={tw_result['id']}")

    rp_result = poster.reply_tweet("リプライテスト", tw_result["id"])
    assert rp_result.get("id", "").startswith("dry_"), f"id={rp_result.get('id')}"
    ok(f"reply_tweet: id={rp_result['id']}")

    metrics = poster.get_metrics(tw_result["id"])
    assert "like_count" in metrics, f"metrics={metrics}"
    ok(f"get_metrics: likes={metrics['like_count']}")

    # post_item — DRY-RUNポスターを渡す（実際のX APIを呼ばない）
    if not main_body:
        main_body  = "テスト本文です #テスト"
    reply_for_post = reply_body or "詳細はこちら https://affiliate.dmm.com/test"

    test_item_for_post = demo_item if isinstance(demo_item, dict) else {
        "product_id": "test001", "title": "テスト", "affiliate_url": ""
    }

    result = post_item(
        item       = test_item_for_post,
        main_body  = main_body,
        reply_body = reply_for_post,
        video_path = None,
        poster     = poster,   # ← DRY-RUNポスターを直接渡す
    )

    assert result.get("tweet_id", "").startswith("dry_"), f"tweet_id={result.get('tweet_id')}"
    ok(f"post_item: tweet_id={result['tweet_id']}, reply_id={result['reply_tweet_id']}")

except Exception as e:
    ng("XPoster DRY-RUN", e)
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────
# 7. mark_posted テスト
# ─────────────────────────────────────────────
section("7. 投稿記録（mark_posted）")
try:
    from core.database import mark_posted

    if queue_id and conn:
        log_id = mark_posted(
            conn         = conn,
            queue_id     = queue_id,
            tweet_id     = "dry_9999999",
            reply_tweet_id = "dry_reply_9999999",
            body         = main_body or "テスト",
            product_id   = "test001",
            post_type    = "sale",
            variant_id   = "A",
            has_image    = False,
            hashtags     = "#テスト",
            video_path   = "",
        )
        if log_id:
            ok(f"mark_posted: log_id={log_id}")
        else:
            ng("mark_posted", "0かNoneが返りました")
    else:
        _safe_print("  [SKIP] queue_idがないためスキップ")

except Exception as e:
    ng("mark_posted", e)
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────
# 8. 動画管理テスト
# ─────────────────────────────────────────────
section("8. 動画管理（video_manager）")
try:
    from core.video_manager import initialize, pick_video, show_library
    from config import VIDEO_DIR

    initialize(conn)
    ok("video_manager.initialize 完了")

    video = pick_video("hitoduma", conn)
    if video:
        ok(f"pick_video: {video}")
    else:
        _safe_print(f"  [INFO] 動画なし（{VIDEO_DIR} に .mp4/.mov を置くと自動登録されます）")
        ok("pick_video: 動画なし（正常）")

except Exception as e:
    ng("video_manager", e)
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────
# 結果集計
# ─────────────────────────────────────────────
_safe_print(f"\n{'='*50}")
total = _pass + _fail
_safe_print(f"  結果: {_pass}/{total} 合格  (失敗: {_fail}件)")
_safe_print(f"{'='*50}")

if conn:
    conn.close()

sys.exit(0 if _fail == 0 else 1)
