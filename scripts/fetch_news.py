#!/usr/bin/env python3
"""
Забирает новые посты канала у Telegram-бота и обновляет ленту новостей лаунчера.

Как это работает:
  • бот — администратор канала, Telegram присылает ему каждый пост как событие channel_post;
  • события хранятся у Telegram 24 часа — скрипт запускается каждые 5 минут (GitHub Actions) и забирает их;
  • посты складываются в news/news.json, картинки — в news/media/;
  • GitHub Pages раздаёт их лаунчеру по адресу https://launcher.dizermoney.ru/news/news.json.

Токен бота берётся только из переменной окружения TELEGRAM_BOT_TOKEN (секрет GitHub) и никуда не записывается.
Нужен только стандартный Python 3.9+, без дополнительных пакетов.

Переменные окружения:
  TELEGRAM_BOT_TOKEN — токен бота (обязательно)
  DELETE_POST        — номер поста, который нужно убрать из ленты (необязательно)
  CHECK_BOT=1        — проверить, что бот видит канал и является администратором
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CHANNEL = "paulDizerGames"
# Бот, созданный только для новостей. Скрипт откажется работать с токеном другого бота:
# чужой бот (например, другой ваш бот на aiogram) потерял бы свои сообщения из общей очереди getUpdates.
BOT_USERNAME = "DizerGamesNewsBot"
MAX_POSTS = 50                      # сколько последних постов держать в ленте
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # больше не скачиваем (Bot API отдаёт файлы до 20 МБ)
PREFERRED_IMAGE_WIDTH = 1280        # из нескольких размеров фото берём ближайший к этому
ENTITY_TYPES = {"bold", "italic", "underline", "strikethrough", "code", "pre",
                "text_link", "url", "mention", "hashtag", "spoiler", "blockquote"}

ROOT = Path(__file__).resolve().parent.parent
NEWS_DIR = ROOT / "news"
MEDIA_DIR = NEWS_DIR / "media"
FEED_PATH = NEWS_DIR / "news.json"
STATE_PATH = NEWS_DIR / "state.json"


class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"Telegram API {code}: {description}")
        self.code = code


# ---------------------------------------------------------------- Bot API

def api(token, method, **params):
    """Вызов метода Bot API. Повторяет запрос при сбоях сети и ответе 429."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (list, dict, bool)) else v for k, v in params.items()}
    ).encode()
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, data=data, timeout=60) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as e:
            payload = json.load(e)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  сеть: {e}, повтор через {2 ** attempt} с")
            time.sleep(2 ** attempt)
            continue

        if payload.get("ok"):
            return payload["result"]
        code = payload.get("error_code")
        if code == 429:
            wait = payload.get("parameters", {}).get("retry_after", 5)
            print(f"  Telegram просит подождать {wait} с")
            time.sleep(wait)
            continue
        # Не печатаем url — в нём токен
        raise TelegramError(code, payload.get("description"))
    raise TelegramError(0, f"{method}: не удалось достучаться до Telegram")


def download_file(token, file_id, destination):
    """Скачать файл по file_id. Ссылка на файл содержит токен, поэтому храним копию у себя."""
    info = api(token, "getFile", file_id=file_id)
    if info.get("file_size", 0) > MAX_IMAGE_BYTES or "file_path" not in info:
        return False
    url = f"https://api.telegram.org/file/bot{token}/{info['file_path']}"
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return True


# ---------------------------------------------------------------- Данные

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def iso(unix_time):
    return datetime.fromtimestamp(unix_time, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def pick_photo(sizes):
    """Из вариантов размера фото выбираем подходящий для ленты: не больше 1280 по ширине, иначе самый маленький из крупных."""
    fitting = [s for s in sizes if s.get("width", 0) <= PREFERRED_IMAGE_WIDTH]
    return max(fitting, key=lambda s: s["width"]) if fitting else min(sizes, key=lambda s: s["width"])


def message_image(message):
    """Картинка сообщения: фото или превью видео/гифки/документа. None, если картинки нет."""
    if message.get("photo"):
        return pick_photo(message["photo"])
    for kind in ("video", "animation", "document"):
        thumb = (message.get(kind) or {}).get("thumbnail")
        if thumb:
            return thumb
    return None


def clean_entities(entities):
    result = []
    for e in entities or []:
        if e.get("type") not in ENTITY_TYPES:
            continue
        item = {"type": e["type"], "offset": e["offset"], "length": e["length"]}
        if e["type"] == "text_link":
            url = e.get("url", "")
            if not url.startswith(("https://", "http://")):
                continue
            item["url"] = url
        result.append(item)
    return result


# ---------------------------------------------------------------- Лента

class Feed:
    def __init__(self, data):
        self.posts = data.get("posts", [])

    def find(self, message):
        """Пост, к которому относится сообщение: по номеру или по альбому (media_group_id)."""
        group = message.get("media_group_id")
        for post in self.posts:
            if message["message_id"] in post.get("messageIds", [post["id"]]):
                return post
            if group and post.get("mediaGroupId") == group:
                return post
        return None

    def apply(self, token, message, edited):
        text = message.get("text") or message.get("caption") or ""
        entities = clean_entities(message.get("entities") or message.get("caption_entities"))
        image = message_image(message)

        post = self.find(message)
        if post is None:
            if not text and not image:
                return False  # опросы, стикеры, служебные сообщения — пропускаем
            post = {
                "id": message["message_id"],
                "date": iso(message["date"]),
                "text": "",
                "entities": [],
                "images": [],
                "link": f"https://t.me/{CHANNEL}/{message['message_id']}",
                "edited": False,
                "messageIds": [message["message_id"]],
            }
            if message.get("media_group_id"):
                post["mediaGroupId"] = message["media_group_id"]
            self.posts.append(post)
        elif message["message_id"] not in post["messageIds"]:
            post["messageIds"].append(message["message_id"])
            # Альбом: первым считается сообщение с меньшим номером
            if message["message_id"] < post["id"]:
                post["id"] = message["message_id"]
                post["date"] = iso(message["date"])
                post["link"] = f"https://t.me/{CHANNEL}/{message['message_id']}"

        # В альбоме подпись обычно только у одного сообщения — не затираем её пустой
        if text or edited:
            if text or not post["text"]:
                post["text"] = text
                post["entities"] = entities
        if edited:
            post["edited"] = True

        if image:
            name = f"{image['file_unique_id']}.jpg"
            post["images"] = [i for i in post["images"] if i.get("messageId") != message["message_id"]]
            if (MEDIA_DIR / name).exists() or download_file(token, image["file_id"], MEDIA_DIR / name):
                post["images"].append({
                    "url": f"news/media/{name}",
                    "width": image.get("width", 0),
                    "height": image.get("height", 0),
                    "messageId": message["message_id"],
                })
                post["images"].sort(key=lambda i: i["messageId"])
        return True

    def delete(self, post_id):
        before = len(self.posts)
        self.posts = [p for p in self.posts if p["id"] != post_id and post_id not in p.get("messageIds", [])]
        return len(self.posts) != before

    def trim(self):
        self.posts.sort(key=lambda p: (p["date"], p["id"]), reverse=True)
        self.posts = self.posts[:MAX_POSTS]

    def to_json(self, updated_at):
        return {"schema": 1, "updatedAt": updated_at, "channel": CHANNEL, "posts": self.posts}


def remove_unused_media(feed):
    used = {Path(i["url"]).name for p in feed.posts for i in p["images"]}
    for file in MEDIA_DIR.glob("*") if MEDIA_DIR.exists() else []:
        if file.name not in used:
            file.unlink()
            print(f"  удалена картинка {file.name}")


# ---------------------------------------------------------------- Запуск

def check_bot(token, me):
    """Самопроверка: видит ли бот канал. Посты приходят, только если бот — администратор."""
    try:
        chat = api(token, "getChat", chat_id=f"@{CHANNEL}")
        print(f"Канал: «{chat.get('title')}», тип {chat.get('type')}, id {chat.get('id')}")
        admins = api(token, "getChatAdministrators", chat_id=chat["id"])
        names = [a["user"].get("username") or a["user"].get("first_name") for a in admins]
        print(f"Администраторы, видимые боту: {', '.join(map(str, names))}")
        if any(a["user"]["id"] == me["id"] for a in admins):
            print("Бот — администратор канала: всё в порядке")
            return
    except TelegramError as e:
        print(f"  getChat/getChatAdministrators: {e}")
    try:
        member = api(token, "getChatMember", chat_id=f"@{CHANNEL}", user_id=me["id"])
        status = member.get("status")
        print(f"Статус в @{CHANNEL}: {status}")
        if status not in ("administrator", "creator"):
            print("ВНИМАНИЕ: бот не администратор канала — новые посты к нему не придут.")
    except TelegramError as e:
        print(f"ВНИМАНИЕ: бот не видит канал @{CHANNEL} ({e}). Добавьте его администратором.")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Не задан TELEGRAM_BOT_TOKEN (секрет репозитория)")
        return 1

    me = api(token, "getMe")
    print(f"Бот: @{me['username']}")
    if me["username"].lower() != BOT_USERNAME.lower():
        print(f"ОШИБКА: в секрете TELEGRAM_BOT_TOKEN токен бота @{me['username']}, а нужен @{BOT_USERNAME}.")
        print("Ничего не делаю, чтобы не забрать чужие сообщения. Обновите секрет: gh secret set TELEGRAM_BOT_TOKEN")
        return 1
    if os.environ.get("CHECK_BOT") == "1":
        check_bot(token, me)

    old = load_json(FEED_PATH, {"posts": []})
    feed = Feed(json.loads(json.dumps(old)))
    state = load_json(STATE_PATH, {"offset": 0})
    changed = False

    delete_id = os.environ.get("DELETE_POST", "").strip()
    if delete_id:
        if feed.delete(int(delete_id)):
            print(f"Пост {delete_id} убран из ленты")
            changed = True
        else:
            print(f"Поста {delete_id} нет в ленте")

    try:
        updates = api(token, "getUpdates", offset=state["offset"], timeout=0,
                      allowed_updates=["channel_post", "edited_channel_post"])
    except TelegramError as e:
        if e.code != 409:
            raise
        # У бота включён webhook — тогда getUpdates не работает. Отключаем webhook, события не теряются.
        print("У бота был включён webhook — отключаю")
        api(token, "deleteWebhook", drop_pending_updates=False)
        updates = api(token, "getUpdates", offset=state["offset"], timeout=0,
                      allowed_updates=["channel_post", "edited_channel_post"])

    print(f"Новых событий: {len(updates)}")
    for update in updates:
        state["offset"] = update["update_id"] + 1
        message = update.get("channel_post") or update.get("edited_channel_post")
        if not message:
            continue
        username = (message.get("chat", {}).get("username") or "").lower()
        if username != CHANNEL.lower():
            print(f"  пропуск: пост из другого канала @{username}")
            continue
        edited = "edited_channel_post" in update
        if feed.apply(token, message, edited):
            print(f"  {'изменён' if edited else 'новый'} пост {message['message_id']}")
            changed = True

    feed.trim()
    if changed or not FEED_PATH.exists():
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        save_json(FEED_PATH, feed.to_json(updated_at))
        remove_unused_media(feed)
        print(f"Лента сохранена: {len(feed.posts)} постов")
    else:
        print("Лента не изменилась")

    if updates:
        # Подтверждаем Telegram, что события получены: следующий вызов начнётся с нового offset
        save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
