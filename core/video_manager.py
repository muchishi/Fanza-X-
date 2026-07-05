"""
core/video_manager.py — 動画ファイル管理（DB連携方式）

動画ファイル本体はディスクに置き、メタデータ（パス・ジャンル・使用回数・
Likes実績など）をSQLiteで管理する。

フォルダ構成:
    data/videos/           — ジャンル共通動画
    data/videos/hitoduma/  — 人妻ジャンル専用
    data/videos/ntr/       — NTRジャンル専用
    ...

DB管理のメリット:
  - 使用回数・Likes実績をもとにパフォーマンス順でローテーション
  - ジャンル別絞り込みがSQLで簡単
  - ファイルが削除されたら自動的に非アクティブ化
  - 起動時に data/videos/ を走査して自動登録（フォルダに置くだけでOK）
"""
import logging
from pathlib import Path
from typing import Optional

from core.database import (
    get_conn, sync_videos_from_disk,
    pick_video_from_db, mark_video_used,
    list_videos_db,
)

log = logging.getLogger(__name__)


def initialize(conn=None) -> dict:
    """
    起動時に呼ぶ。data/videos/ を走査してDB未登録の動画を自動登録する。

    Returns:
        {"added": N, "already_registered": M, "missing": K}
    """
    _conn = conn or get_conn()
    stats = sync_videos_from_disk(_conn)
    if not conn:
        _conn.close()

    if stats["added"] > 0:
        log.info("[動画DB] 新規登録: %d本", stats["added"])
    if stats["missing"] > 0:
        log.warning("[動画DB] ファイル消失で非アクティブ化: %d本", stats["missing"])

    return stats


def pick_video(genre_key: str = "", conn=None) -> Optional[Path]:
    """
    次に使う動画をDBから選んでPathを返す。

    Args:
        genre_key: FANZAジャンルキー。ジャンル別動画を優先選択。
        conn:      既存のDBコネクション（Noneなら新規作成）

    Returns:
        動画ファイルのPath。登録動画がなければNone。
    """
    _conn = conn or get_conn()

    row = pick_video_from_db(_conn, genre_key)
    if not conn:
        _conn.close()

    if not row:
        log.warning(
            "[動画] 動画が登録されていません。"
            "data/videos/ にMP4を置いて python main.py --sync-videos を実行してください。"
        )
        return None

    path = Path(row["file_path"])
    if not path.exists():
        log.warning("[動画] ファイルが見つかりません: %s", path)
        return None

    log.info("[動画] 選択: %s (使用回数=%d)", path.name, row["used_count"])
    return path


def record_video_result(video_path: Path, likes: int = 0, conn=None) -> None:
    """
    投稿後にこの動画のLikes実績をDBに記録する。
    エンゲージメント収集タスク（task_collect_metrics）から呼ぶ。
    """
    _conn = conn or get_conn()

    row = _conn.execute(
        "SELECT id FROM videos WHERE file_path = ?", (str(video_path),)
    ).fetchone()

    if row:
        mark_video_used(_conn, row["id"], likes)

    if not conn:
        _conn.close()


def show_library(conn=None) -> None:
    """登録済み動画の一覧をコンソールに出力（確認用）"""
    _conn = conn or get_conn()
    videos = list_videos_db(_conn)
    if not conn:
        _conn.close()

    if not videos:
        print("動画が登録されていません。")
        print("data/videos/ にMP4ファイルを配置して --sync-videos を実行してください。")
        return

    print(f"\n{'ID':>4}  {'ファイル名':<30}  {'ジャンル':<12}  {'サイズ(KB)':>10}  {'使用回数':>8}  {'avg_likes':>9}  {'状態'}")
    print("-" * 95)
    for v in videos:
        genre  = v["genre_key"] or "共通"
        active = "✅" if v["active"] else "⛔"
        print(f"{v['id']:>4}  {v['filename']:<30}  {genre:<12}  "
              f"{v['file_size_kb']:>10,}  {v['used_count']:>8}  "
              f"{v['avg_likes']:>9.1f}  {active}")
    print(f"\n合計: {len(videos)}本")


if __name__ == "__main__":
    import sys
    from core.database import init_db
    init_db()
    conn = get_conn()
    stats = initialize(conn)
    print(f"同期完了: 追加={stats['added']} / 登録済み={stats['already_registered']} / 消失={stats['missing']}")
    show_library(conn)
    conn.close()
