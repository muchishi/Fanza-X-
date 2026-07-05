"""
poster/telegram_notifier.py — 下書きをTelegramへ通知送信

X APIを使わない半自動投稿モード(task_draft)用の配信手段。
下書き作成時に本文・画像/動画・リプライ文をTelegramに送ることで、
スマホのTelegramアプリからコピー&ペーストでX投稿できるようにする。

セットアップ:
  1. Telegramで @BotFather に /newbot を送信してBotトークンを取得
  2. 作成したBotに任意のメッセージを1通送信する
  3. https://api.telegram.org/bot<トークン>/getUpdates を開き、
     "chat":{"id": ...} の値がchat_id
  4. .env に TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID を設定
"""
import sys
import json
import logging
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import TELEGRAM

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}"


def _enabled() -> bool:
    return bool(TELEGRAM.get("bot_token") and TELEGRAM.get("chat_id") and HAS_REQUESTS)


def send_message(text: str) -> bool:
    if not _enabled():
        return False
    url = API_BASE.format(token=TELEGRAM["bot_token"]) + "/sendMessage"
    try:
        resp = requests.post(
            url, data={"chat_id": TELEGRAM["chat_id"], "text": text}, timeout=15
        )
        if resp.status_code != 200:
            log.warning("[Telegram] sendMessage失敗: %d %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("[Telegram] sendMessage例外: %s", e)
        return False


def send_video(video_path, caption: str) -> bool:
    if not _enabled():
        return False
    url = API_BASE.format(token=TELEGRAM["bot_token"]) + "/sendVideo"
    try:
        with open(video_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": TELEGRAM["chat_id"], "caption": caption},
                files={"video": f},
                timeout=60,
            )
        if resp.status_code != 200:
            log.warning("[Telegram] sendVideo失敗: %d %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("[Telegram] sendVideo例外: %s", e)
        return False


def send_photo_group(image_paths: list, caption: str) -> bool:
    if not _enabled() or not image_paths:
        return False

    url = API_BASE.format(token=TELEGRAM["bot_token"]) + "/sendMediaGroup"
    media = []
    files = {}
    opened = []
    try:
        for i, path in enumerate(image_paths[:10]):
            key = f"photo{i}"
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0:
                item["caption"] = caption
            media.append(item)
            fh = open(path, "rb")
            opened.append(fh)
            files[key] = fh

        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM["chat_id"], "media": json.dumps(media)},
            files=files,
            timeout=60,
        )
        if resp.status_code != 200:
            log.warning("[Telegram] sendMediaGroup失敗: %d %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("[Telegram] sendMediaGroup例外: %s", e)
        return False
    finally:
        for fh in opened:
            fh.close()


def notify_draft(
    main_body: str,
    reply_body: str,
    queue_id: int,
    video_path=None,
    image_paths: Optional[list] = None,
) -> bool:
    """
    下書き1件をTelegramに通知する。
    メディア付きメッセージ(本文=キャプション) → リプライ文 → 確認コマンド案内、の順で送信。
    """
    if not _enabled():
        log.debug("[Telegram] 未設定のため通知スキップ")
        return False

    if video_path:
        ok = send_video(video_path, caption=main_body)
    elif image_paths:
        ok = send_photo_group(image_paths, caption=main_body)
    else:
        ok = send_message(main_body)

    if reply_body:
        send_message(f"【リプライ用】\n{reply_body}")

    send_message(f"投稿し終えたら実行:\npython main.py --confirm-posted {queue_id}")

    return ok
