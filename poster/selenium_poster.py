"""
poster/selenium_poster.py — Selenium によるブラウザ自動投稿（X API不使用）

【重要な注意】
XのAutomation Rulesは自動投稿には公式APIの利用を求めており、
本モジュールのようなブラウザ自動操作での投稿は利用規約違反となり、
アカウント凍結（最悪の場合デバイス単位でのban evasion判定）のリスクを伴う。
利用は自己責任で行うこと。

【使い方】
初回のみ手動ログインが必要:
    python main.py --selenium-login
ブラウザが起動するので、X (Twitter) に手動でログインし、
ログイン後にターミナルでEnterキーを押す。
以降は data/chrome_profile/ にログイン状態が保存され、自動で再利用される。

【壊れやすさについて】
X側のフロントエンド（DOM構造・data-testid属性）は予告なく変更されることがある。
本モジュールのセレクタが機能しなくなった場合は、実際のページのDOMを確認し
セレクタを更新する必要がある。
"""
import sys
import time
import logging
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import SELENIUM

log = logging.getLogger(__name__)

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

BASE_URL    = "https://x.com"
COMPOSE_URL = BASE_URL + "/compose/post"
LOGIN_URL   = BASE_URL + "/i/flow/login"

_driver = None  # モジュール単位のシングルトン（起動コストが高いため使い回す）

SEL_NEW_POST_BTN   = "a[data-testid=SideNav_NewTweet_Button]"
SEL_TEXTAREA       = "div[data-testid=tweetTextarea_0]"
SEL_FILE_INPUT     = "input[data-testid=fileInput]"
SEL_ATTACHMENTS    = "[data-testid=attachments]"
SEL_POST_BTN       = "button[data-testid=tweetButton]"
SEL_REPLY_BTN      = "button[data-testid=tweetButtonInline]"
SEL_TWEET_LINK     = "article[data-testid=tweet] a[href*='/status/']"


# ----------------------------------------------
# ドライバ管理
# ----------------------------------------------
def _build_driver(headless: Optional[bool] = None):
    if not HAS_SELENIUM:
        raise RuntimeError("selenium が未インストールです。pip install selenium を実行してください。")

    profile_dir = Path(SELENIUM["profile_dir"])
    profile_dir.mkdir(parents=True, exist_ok=True)

    opts = Options()
    opts.add_argument("--user-data-dir=" + str(profile_dir))
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    use_headless = SELENIUM.get("headless", False) if headless is None else headless
    if use_headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,1000")

    driver = webdriver.Chrome(options=opts)
    driver.implicitly_wait(0)
    return driver


def get_driver():
    """プロセス内で使い回すドライバを返す（なければ起動する）"""
    global _driver
    if _driver is None:
        _driver = _build_driver()
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None


def _wait(driver):
    return WebDriverWait(driver, SELENIUM.get("wait_timeout_sec", 20))


# ----------------------------------------------
# ログイン確認・手動ログインフロー
# ----------------------------------------------
def is_logged_in(driver) -> bool:
    """ホーム画面に投稿ボタンが出ていればログイン済みと判定する"""
    driver.get(BASE_URL + "/home")
    try:
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SEL_NEW_POST_BTN))
        )
        return True
    except TimeoutException:
        return False


def _has_session_cookie(driver) -> bool:
    """
    auth_token Cookieの有無でログイン判定する。
    is_logged_in()と違いページ遷移を伴わないため、
    ユーザーが手動でログイン操作中のページを妨げない。
    """
    try:
        cookies = driver.get_cookies()
    except Exception:
        return False
    return any(c.get("name") == "auth_token" for c in cookies)


def interactive_login(timeout_sec: int = 600) -> bool:
    """
    初回セットアップ用: ヘッドあり(画面表示)ブラウザでログイン画面を開き、
    ログイン完了をポーリングで自動検知する（input()は使わない。
    バックグラウンド実行環境では標準入力を受け付けられないため）。

    ポーリング中はページ遷移を発生させない（Cookie確認のみ）ことで、
    ユーザーがブラウザで手動ログイン操作している最中に妨げないようにする。
    """
    driver = _build_driver(headless=False)
    try:
        driver.get(LOGIN_URL)
        print(f"ブラウザでXにログインしてください。最大{timeout_sec}秒待機します。")
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                if _has_session_cookie(driver):
                    print("ログイン状態を確認できました。data/chrome_profile/ に保存されます。")
                    return True
            except Exception as e:
                log.debug("[Selenium] ログイン確認中の一時的なエラー: %s", e)
            time.sleep(5)
        print("タイムアウトしました。もう一度 --selenium-login を実行してください。")
        return False
    finally:
        driver.quit()


# ----------------------------------------------
# 投稿
# ----------------------------------------------
def _type_multiline(el, text: str) -> None:
    for i, line in enumerate(text.split("\n")):
        if i > 0:
            el.send_keys(Keys.SHIFT, Keys.ENTER)
        el.send_keys(line)


def _upload_media(driver, media_paths: list) -> None:
    file_input = _wait(driver).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, SEL_FILE_INPUT))
    )
    joined = "\n".join(str(p) for p in media_paths)
    file_input.send_keys(joined)
    _wait(driver).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, SEL_ATTACHMENTS))
    )
    time.sleep(2)


def _latest_tweet_url(driver, username: str) -> Optional[str]:
    """プロフィールページの先頭ツイート(=直近投稿)のURLを取得する"""
    driver.get(BASE_URL + "/" + username)
    try:
        link = _wait(driver).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SEL_TWEET_LINK))
        )
        return link.get_attribute("href")
    except TimeoutException:
        return None


def post_tweet(driver, text: str, media_paths: Optional[list] = None) -> Optional[str]:
    """メインツイートを投稿してツイートURLを返す（失敗時はNone）。"""
    username = SELENIUM.get("username", "")
    driver.get(COMPOSE_URL)

    textarea = _wait(driver).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, SEL_TEXTAREA))
    )
    textarea.click()
    _type_multiline(textarea, text)

    if media_paths:
        _upload_media(driver, media_paths)

    post_btn = _wait(driver).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_POST_BTN))
    )
    post_btn.click()
    time.sleep(4)

    if not username:
        log.warning("[Selenium] SELENIUM['username'] が未設定のため投稿URLを特定できません")
        return None
    return _latest_tweet_url(driver, username)


def reply_tweet(driver, tweet_url: str, text: str) -> Optional[str]:
    """指定ツイートへのリプライを投稿してリプライURLを返す（失敗時はNone）。"""
    username = SELENIUM.get("username", "")
    driver.get(tweet_url)

    textarea = _wait(driver).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, SEL_TEXTAREA))
    )
    textarea.click()
    _type_multiline(textarea, text)

    reply_btn = _wait(driver).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_REPLY_BTN))
    )
    reply_btn.click()
    time.sleep(4)

    if not username:
        return None
    return _latest_tweet_url(driver, username)


def _extract_status_id(tweet_url: Optional[str]) -> str:
    if not tweet_url:
        return ""
    return tweet_url.rstrip("/").split("/")[-1]


# ----------------------------------------------
# main.py から呼ぶ統合関数（post_item と同じインターフェース）
# ----------------------------------------------
def post_item_selenium(
    item: dict,
    main_body: str,
    reply_body: str,
    video_path=None,
    image_urls: Optional[list] = None,
    variant_id: str = "A",
) -> dict:
    from poster.x_poster import download_image

    driver = get_driver()

    if not is_logged_in(driver):
        raise RuntimeError(
            "Xにログインしていません。python main.py --selenium-login を先に実行してください。"
        )

    media_paths = []
    tmp_files = []
    if video_path:
        media_paths = [str(video_path)]
    elif image_urls:
        for i, url in enumerate(image_urls[:4]):
            img_bytes = download_image(url)
            if not img_bytes:
                continue
            ext = Path(url).suffix or ".jpg"
            tmp = Path(tempfile.gettempdir()) / ("selenium_upload_" + str(int(time.time())) + "_" + str(i) + ext)
            tmp.write_bytes(img_bytes)
            tmp_files.append(tmp)
            media_paths.append(str(tmp))

    try:
        tweet_url = post_tweet(driver, main_body, media_paths=media_paths or None)
        reply_url = None
        if reply_body and tweet_url:
            time.sleep(2)
            reply_url = reply_tweet(driver, tweet_url, reply_body)
    finally:
        for f in tmp_files:
            try:
                f.unlink()
            except Exception:
                pass

    return {
        "tweet_id":       _extract_status_id(tweet_url),
        "reply_tweet_id": _extract_status_id(reply_url),
        "has_video":      bool(video_path),
        "has_image":      bool(media_paths) and not video_path,
        "video_path":     str(video_path) if video_path else "",
        "variant_id":     variant_id,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="手動ログインしてセッションを保存")
    args = parser.parse_args()
    if args.login:
        interactive_login()
