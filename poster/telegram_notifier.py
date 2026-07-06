"""
poster/telegram_notifier.py — 下書きをTelegramへ通知送信

X APIを使わない半自動投稿モード(task_draft)用の配信手段。
下書き作成時に本文・画像/動画・リプライ文をTelegramに送ることで、
スマホのTelegramアプリからコピー&ペーストでX投稿できるようにする。
「✅ 投稿完了」ボタンをタップすると自動でconfirm_posted()が実行される
（main.py --telegram-listen またはスケジューラー実行中のみ）。

画像系の下書き(同人誌など)は3投稿スレッドとして案内する:
  1投稿目: 本文（先頭に(1/3)）+ 画像1枚
  2投稿目: 残りの画像のみ（リプライ）
  3投稿目: アフィリエイトリンク（リプライ）
動画系の下書き(FANZA動画作品)は従来通り2投稿（動画+本文 → リプライにリンク）。

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


def send_message_with_confirm_button(text: str, queue_id: int) -> bool:
    """「✅ 投稿完了」ボタン付きメッセージを送る。タップするとcallback_queryが飛ぶ"""
    if not _enabled():
        return False
    url = API_BASE.format(token=TELEGRAM["bot_token"]) + "/sendMessage"
    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ 投稿完了", "callback_data": "confirm:{}".format(queue_id)}
        ]]
    }
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM["chat_id"],
                "text": text,
                "reply_markup": json.dumps(reply_markup),
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("[Telegram] sendMessage(button)失敗: %d %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("[Telegram] sendMessage(button)例外: %s", e)
        return False


def send_video(video_path, caption: str = "") -> bool:
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


def send_photo(image_path, caption: str = "") -> bool:
    if not _enabled():
        return False
    url = API_BASE.format(token=TELEGRAM["bot_token"]) + "/sendPhoto"
    try:
        with open(image_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": TELEGRAM["chat_id"], "caption": caption},
                files={"photo": f},
                timeout=30,
            )
        if resp.status_code != 200:
            log.warning("[Telegram] sendPhoto失敗: %d %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("[Telegram] sendPhoto例外: %s", e)
        return False


def send_photo_group(image_paths: list, caption: str = "") -> bool:
    """2枚以上の画像をまとめて送る（TelegramのsendMediaGroup仕様上、1枚では使えない）"""
    if not _enabled() or not image_paths:
        return False

    url = API_BASE.format(token=TELEGRAM["bot_token"]) + "/sendMediaGroup"
    media = []
    files = {}
    opened = []
    try:
        for i, path in enumerate(image_paths[:10]):
            key = "photo{}".format(i)
            item = {"type": "photo", "media": "attach://{}".format(key)}
            if i == 0 and caption:
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


def _send_photos_any(image_paths: list, caption: str = "") -> bool:
    """画像1枚ならsendPhoto、2枚以上ならsendMediaGroupを使う"""
    if not image_paths:
        return False
    if len(image_paths) == 1:
        return send_photo(image_paths[0], caption=caption)
    return send_photo_group(image_paths, caption=caption)


def get_updates(offset: Optional[int] = None, timeout: int = 5) -> list:
    """long-polling で新着メッセージ/コールバックを取得する"""
    if not _enabled():
        return []
    url = API_BASE.format(token=TELEGRAM["bot_token"]) + "/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=timeout + 10)
        if resp.status_code != 200:
            log.warning("[Telegram] getUpdates失敗: %d %s", resp.status_code, resp.text[:200])
            return []
        return resp.json().get("result", [])
    except Exception as e:
        log.warning("[Telegram] getUpdates例外: %s", e)
        return []


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    if not _enabled():
        return
    url = API_BASE.format(token=TELEGRAM["bot_token"]) + "/answerCallbackQuery"
    try:
        requests.post(
            url,
            data={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning("[Telegram] answerCallbackQuery例外: %s", e)


def notify_draft(
    main_body: str,
    reply_body: str,
    queue_id: int,
    video_path=None,
    image_paths: Optional[list] = None,
) -> bool:
    """
    動画系(FANZA動画作品)の下書き1件をTelegramに通知する（従来通りの2投稿構成）。
    メディア付きメッセージ(本文=キャプション) → リプライ文 → 「投稿完了」ボタン、の順で送信。
    """
    if not _enabled():
        log.debug("[Telegram] 未設定のため通知スキップ")
        return False

    if video_path:
        ok = send_video(video_path, caption=main_body)
    elif image_paths:
        ok = _send_photos_any(image_paths, caption=main_body)
    else:
        ok = send_message(main_body)

    if reply_body:
        send_message(reply_body)

    send_message_with_confirm_button(
        "投稿し終えたらボタンを押してください👇" + chr(10) + "(または python main.py --confirm-posted {})".format(queue_id),
        queue_id,
    )

    return ok


def notify_draft_thread(
    part1_text: str,
    part1_image,
    part2_images: list,
    part3_text: str,
    queue_id: int,
    part2_caption: str = "",
) -> bool:
    """
    画像系の下書きを3投稿スレッドとして案内する。
    X側の投稿は3分割(1投稿目:本文+画像1枚 / 2投稿目:残り画像 / 3投稿目:リンク)のままだが、
    Telegramでの画像確認・保存をしやすくするため、画像は4枚まとめて1回で送る。
    どれが1投稿目用の画像かは、後続の part2_caption（(2/3)など）のテキストで判別する。
    """
    if not _enabled():
        log.debug("[Telegram] 未設定のため通知スキップ")
        return False

    all_images = ([part1_image] if part1_image else []) + list(part2_images)
    if all_images:
        ok = _send_photos_any(all_images, caption=part1_text)
    else:
        ok = send_message(part1_text)

    if part2_caption:
        send_message(part2_caption)

    if part3_text:
        send_message(part3_text)

    send_message_with_confirm_button(
        "3投稿すべて完了したらボタンを押してください👇" + chr(10) + "(または python main.py --confirm-posted {})".format(queue_id),
        queue_id,
    )

    return ok
