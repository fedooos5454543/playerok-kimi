"""
Telegram-бот для продавца на Playerok. Всё в одном файле.

Что делает:
  1. При продаже товара — присылает в Telegram сообщение с названием товара,
     ценой и ником покупателя.
  2. Пересылает в Telegram входящие сообщения от покупателей из чатов Playerok.
  3. Уведомляет о новых отзывах.
  4. (Опционально) Автоматически пересоздаёт и заново публикует проданный товар.
  5. Автопубликация по расписанию.
  6. Отчёты по продажам и пиковые часы.
  7. Команды: /items, /chats, /reply, /report, /peakhours, /autopub

Запуск:
    pip install -r requirements.txt
    cp .env.example .env   # и заполнить своими данными
    python main.py
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
import requests

from playerokapi.account import Account
from playerokapi.enums import EventTypes
from playerokapi.listener.listener import EventListener
from playerokapi.exceptions import BotCheckDetectedException, UnauthorizedError


# =========================================================================
#  НАСТРОЙКА / ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# =========================================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
PLAYEROK_COOKIES = os.getenv("PLAYEROK_COOKIES", "").strip()
PLAYEROK_USER_AGENT = os.getenv("PLAYEROK_USER_AGENT", "").strip()
AUTO_RELIST = os.getenv("AUTO_RELIST", "false").strip().lower() in ("1", "true", "yes")
RELIST_PRIORITY_STATUS_ID = os.getenv("RELIST_PRIORITY_STATUS_ID") or None

REQUIRED_VARS = {
    "TG_BOT_TOKEN": TG_BOT_TOKEN,
    "TG_CHAT_ID": TG_CHAT_ID,
    "PLAYEROK_COOKIES": PLAYEROK_COOKIES,
    "PLAYEROK_USER_AGENT": PLAYEROK_USER_AGENT,
}


def check_env():
    missing = [k for k, v in REQUIRED_VARS.items() if not v]
    if missing:
        raise SystemExit(
            "Не заполнены переменные окружения: "
            + ", ".join(missing)
            + "\nСкопируйте .env.example в .env и заполните значения."
        )


# =========================================================================
#  СОСТОЯНИЕ БОТА (файл state.json)
# =========================================================================

STATE_FILE = "bot_state.json"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Ошибка загрузки state: %s", e)
    return {
        "seen_deal_ids": [],
        "seen_review_ids": [],
        "sales_log": [],       # {timestamp, item_name, price, buyer}
        "autopub": None,       # {enabled: bool, time: "HH:MM", item_ids: []}
    }


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Ошибка сохранения state: %s", e)


state = load_state()


# =========================================================================
#  TELEGRAM: отправка сообщений + обработка команд
# =========================================================================

class TelegramNotifier:
    """Клиент для отправки сообщений и получения команд через Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
        self.last_update_id = 0

    def _api(self, method: str, payload: dict) -> dict:
        try:
            resp = requests.post(
                f"{self.api_url}/{method}",
                json=payload,
                timeout=15,
            )
            return resp.json()
        except Exception:
            logger.exception("Telegram API error")
            return {}

    def send(self, text: str, disable_preview: bool = True) -> bool:
        data = self._api("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        })
        return data.get("ok", False)

    def get_updates(self) -> list:
        """Получает новые сообщения/команды из Telegram."""
        data = self._api("getUpdates", {
            "offset": self.last_update_id + 1,
            "limit": 10,
        })
        if not data.get("ok"):
            return []
        updates = data.get("result", [])
        for u in updates:
            if u.get("update_id", 0) > self.last_update_id:
                self.last_update_id = u["update_id"]
        return updates

    def handle_command(self, acc, text: str):
        """Обрабатывает текстовые команды из Telegram."""
        text = text.strip()
        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=2)
        cmd = parts[0].lower()
        args = parts[1:]

        # ---------- /items ----------
        if cmd == "/items":
            try:
                user = acc.get_user(username=acc.username)
                item_list = user.get_items()
                if not item_list or not getattr(item_list, "items", None):
                    self.send("📭 У вас нет активных товаров.")
                    return
                lines = ["📦 <b>Ваши товары:</b>\n"]
                for i, item in enumerate(item_list.items[:20], 1):
                    status = "🟢" if getattr(item, "status", "") == "APPROVED" else "🟡"
                    name = getattr(item, "name", "Без названия")
                    price = getattr(item, "price", "?")
                    lines.append(f"{status} {i}. {name} — {price} ₽")
                if len(item_list.items) > 20:
                    lines.append(f"\n...и ещё {len(item_list.items) - 20} товаров")
                self.send("\n".join(lines))
            except Exception as e:
                logger.exception("/items error")
                self.send(f"🔴 Ошибка: {e}")

        # ---------- /chats ----------
        elif cmd == "/chats":
            try:
                chat_list = acc.load_chats(count=15)
                if not chat_list or not getattr(chat_list, "chats", None):
                    self.send("📭 Нет активных чатов.")
                    return
                lines = ["💬 <b>Активные чаты:</b>\n"]
                for chat in chat_list.chats:
                    participant = getattr(chat, "participant", None)
                    name = getattr(participant, "username", "?") if participant else "?"
                    last_msg = getattr(chat, "last_message", None)
                    last_text = getattr(last_msg, "text", "Нет сообщений") if last_msg else "Нет сообщений"
                    lines.append(f"👤 @{name}: {last_text[:40]}...")
                self.send("\n".join(lines))
            except Exception as e:
                logger.exception("/chats error")
                self.send(f"🔴 Ошибка: {e}")

        # ---------- /reply ----------
        elif cmd == "/reply":
            if len(args) < 2:
                self.send(
                    "✏️ <b>Использование:</b>\n"
                    "/reply @username Текст сообщения\n\n"
                    "Пример: /reply IvanPrivet Привет! Товар отправлен."
                )
                return
            username = args[0].lstrip("@")
            message_text = args[1]
            try:
                chat = acc.get_chat_by_username(username)
                if not chat:
                    self.send(f"❌ Чат с @{username} не найден.")
                    return
                acc.send_message(chat_id=getattr(chat, "id"), text=message_text)
                self.send(f"✅ Отправлено @{username}:\n{message_text}")
            except Exception as e:
                logger.exception("/reply error")
                self.send(f"🔴 Ошибка отправки: {e}")

        # ---------- /report ----------
        elif cmd == "/report":
            period = args[0].lower() if args else "day"
            now = datetime.now()
            if period == "day":
                start = now - timedelta(days=1)
                title = "📊 <b>Отчёт за день</b>"
            elif period == "week":
                start = now - timedelta(weeks=1)
                title = "📊 <b>Отчёт за неделю</b>"
            elif period == "month":
                start = now - timedelta(days=30)
                title = "📊 <b>Отчёт за месяц</b>"
            else:
                self.send("Использование: /report day | week | month")
                return

            sales = [s for s in state.get("sales_log", []) if s["timestamp"] >= start.isoformat()]
            total = sum(s["price"] for s in sales)
            count = len(sales)
            top_items = Counter(s["item_name"] for s in sales).most_common(5)
            top_buyers = Counter(s["buyer"] for s in sales).most_common(5)

            lines = [title, f"\n🛒 Продаж: <b>{count}</b>", f"💰 Выручка: <b>{total:,} ₽</b>".replace(",", " ")]
            if top_items:
                lines.append("\n🏆 Топ товаров:")
                for name, c in top_items:
                    lines.append(f"  • {name} — {c} шт.")
            if top_buyers:
                lines.append("\n👥 Топ покупателей:")
                for name, c in top_buyers:
                    lines.append(f"  • @{name} — {c} покупок")
            self.send("\n".join(lines))

        # ---------- /peakhours ----------
        elif cmd == "/peakhours":
            sales = state.get("sales_log", [])
            if not sales:
                self.send("📭 Пока нет данных о продажах.")
                return
            hours = Counter(datetime.fromisoformat(s["timestamp"]).hour for s in sales)
            peak = hours.most_common(5)
            lines = ["⏰ <b>Пиковые часы продаж:</b>\n"]
            for h, c in peak:
                lines.append(f"  🕐 {h:02d}:00 — {c} продаж")
            # Дни недели
            weekdays = Counter(datetime.fromisoformat(s["timestamp"]).strftime("%A") for s in sales)
            lines.append("\n📅 Пиковые дни:")
            for day, c in weekdays.most_common():
                lines.append(f"  • {day} — {c} продаж")
            self.send("\n".join(lines))

        # ---------- /autopub ----------
        elif cmd == "/autopub":
            if not args:
                autopub = state.get("autopub")
                if autopub and autopub.get("enabled"):
                    ids = ", ".join(autopub.get("item_ids", []))
                    self.send(
                        f"🔄 <b>Автопубликация:</b> ВКЛ\n"
                        f"⏰ Время: {autopub.get('time', '?')}\n"
                        f"📦 Товары: {ids or 'все'}\n\n"
                        "Отключить: /autopub off"
                    )
                else:
                    self.send(
                        "🔄 <b>Автопубликация:</b> ВЫКЛ\n\n"
                        "Включить: /autopub on HH:MM [item_id1 item_id2 ...]\n"
                        "Пример: /autopub on 14:00\n"
                        "Пример: /autopub on 09:30 abc123 def456"
                    )
                return

            action = args[0].lower()
            if action == "off":
                state["autopub"] = {"enabled": False, "time": None, "item_ids": []}
                save_state(state)
                self.send("🔄 Автопубликация <b>выключена</b>.")
                return

            if action == "on" and len(args) >= 2:
                time_str = args[1]
                if not re.match(r"^\d{2}:\d{2}$", time_str):
                    self.send("❌ Неверный формат времени. Используйте HH:MM, например 14:00")
                    return
                item_ids = args[2:] if len(args) > 2 else []
                state["autopub"] = {"enabled": True, "time": time_str, "item_ids": item_ids}
                save_state(state)
                ids_text = ", ".join(item_ids) if item_ids else "все товары"
                self.send(f"🔄 Автопубликация <b>включена</b>!\n⏰ Время: {time_str}\n📦 Товары: {ids_text}")
                return

            self.send("Использование: /autopub on HH:MM [item_ids...] | /autopub off")

        # ---------- /help ----------
        elif cmd == "/help":
            self.send(
                "📖 <b>Команды бота:</b>\n\n"
                "/items — список товаров и цен\n"
                "/chats — активные чаты с покупателями\n"
                "/reply @user текст — ответить покупателю на Playerok\n"
                "/report day|week|month — отчёт по продажам\n"
                "/peakhours — пиковые часы и дни продаж\n"
                "/autopub on HH:MM [ids] — автопубликация по расписанию\n"
                "/autopub off — выключить автопубликацию\n"
                "/help — эта справка"
            )


# =========================================================================
#  АВТО-ВЫСТАВЛЕНИЕ ПРОДАННОГО ТОВАРА ЗАНОВО
# =========================================================================

def _download_attachments(acc, attachments) -> list:
    paths = []
    for att in attachments or []:
        try:
            file_bytes = acc.download_file(att.url)
            suffix = os.path.splitext(getattr(att, "filename", ""))[1] or ".jpg"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(file_bytes)
            tmp.close()
            paths.append(tmp.name)
        except Exception:
            logger.exception("Не удалось скачать вложение %s", getattr(att, "url", "?"))
    return paths


def relist_sold_item(acc, sold_item_id: str, priority_status_id: str | None = None):
    original = acc.get_item(id=sold_item_id)
    attachment_paths = _download_attachments(acc, getattr(original, "attachments", None))
    data_fields = getattr(original, "data_fields", None) or []
    options = getattr(original, "attributes", None) or {}

    new_item = acc.create_item(
        game_category_id=original.category.id,
        obtaining_type_id=getattr(getattr(original, "obtaining_type", None), "id", None),
        name=original.name,
        price=original.price,
        description=original.description,
        options=options,
        data_fields=data_fields,
        attachments=attachment_paths,
    )

    for p in attachment_paths:
        try:
            os.remove(p)
        except OSError:
            pass

    statuses = acc.get_item_priority_statuses(new_item.id, new_item.price)
    if priority_status_id:
        status = next((s for s in statuses if s.id == priority_status_id), None)
    else:
        status = next((s for s in statuses if getattr(s, "price", -1) == 0), None)
    if status is None and statuses:
        status = statuses[0]

    if status is None:
        raise RuntimeError("Не удалось найти статус приоритета для публикации")

    published = acc.publish_item(new_item.id, status.id)
    return published


# =========================================================================
#  ОБРАБОТЧИКИ СОБЫТИЙ PLAYEROK
# =========================================================================

def format_price(value) -> str:
    try:
        return f"{int(value):,}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return str(value)


def handle_sale(acc, tg: TelegramNotifier, deal, seen_deal_ids: set):
    if deal.id in seen_deal_ids:
        return
    seen_deal_ids.add(deal.id)

    # Сохраняем в лог для отчётов
    item = deal.item
    buyer = deal.user
    item_name = getattr(item, "name", "неизвестный товар")
    item_price = getattr(item, "price", 0)
    buyer_name = getattr(buyer, "username", "неизвестный")

    state["sales_log"].append({
        "timestamp": datetime.now().isoformat(),
        "item_name": item_name,
        "price": item_price,
        "buyer": buyer_name,
        "deal_id": deal.id,
    })
    save_state(state)

    text = (
        "🛒 <b>Новая продажа!</b>\n\n"
        f"📦 Товар: <b>{item_name}</b>\n"
        f"💰 Цена: {format_price(item_price)}\n"
        f"👤 Покупатель: {buyer_name}\n"
        f"🆔 Сделка: <code>{deal.id}</code>"
    )
    tg.send(text)
    logger.info("Продажа: %s купил(а) '%s' за %s", buyer_name, item_name, item_price)

    if AUTO_RELIST and item is not None:
        try:
            new_item = relist_sold_item(acc, item.id, RELIST_PRIORITY_STATUS_ID)
            tg.send(
                "♻️ Товар автоматически выставлен на продажу заново:\n"
                f"<b>{new_item.name}</b> — {format_price(new_item.price)}\n"
                "⚠️ Рекомендую проверить карточку вручную."
            )
            logger.info("Авто-релист: создан новый предмет %s", new_item.id)
        except Exception:
            logger.exception("Не удалось авто-выставить товар (item_id=%s)", item.id)
            tg.send(
                "⚠️ Не получилось автоматически перевыставить товар "
                f"«{item_name}». Проверьте вручную."
            )


def handle_new_message(acc, tg: TelegramNotifier, event):
    message = event.message
    if message.user.id == acc.id:
        return

    sender = getattr(message.user, "username", "Собеседник")
    text = message.text or "[вложение/изображение]"

    tg.send(f"💬 <b>{sender}</b>:\n{text}")
    logger.info("Новое сообщение от %s: %s", sender, text[:80])


def handle_new_review(acc, tg: TelegramNotifier, event, seen_review_ids: set):
    """Обрабатывает новый отзыв."""
    review = getattr(event, "review", None)
    if not review:
        return
    review_id = getattr(review, "id", None)
    if not review_id or review_id in seen_review_ids:
        return
    seen_review_ids.add(review_id)

    author = getattr(review, "author", None)
    author_name = getattr(author, "username", "Аноним") if author else "Аноним"
    rating = getattr(review, "rating", "?")
    text = getattr(review, "text", "[без текста]") or "[без текста]"
    item = getattr(review, "item", None)
    item_name = getattr(item, "name", "неизвестный товар") if item else "неизвестный товар"

    tg.send(
        "⭐ <b>Новый отзыв!</b>\n\n"
        f"📦 Товар: <b>{item_name}</b>\n"
        f"👤 Автор: {author_name}\n"
        f"⭐️ Оценка: {rating}/5\n"
        f"📝 Отзыв: {text[:200]}"
    )
    logger.info("Новый отзыв от %s на '%s': %s/5", author_name, item_name, rating)


# =========================================================================
#  АВТОПУБЛИКАЦИЯ ПО РАСПИСАНИЮ
# =========================================================================

def autopublish_worker(acc, tg: TelegramNotifier):
    """Фоновый поток: проверяет время и публикует товары."""
    while True:
        time.sleep(60)  # проверяем каждую минуту
        autopub = state.get("autopub")
        if not autopub or not autopub.get("enabled"):
            continue

        now = datetime.now().strftime("%H:%M")
        if now != autopub.get("time"):
            continue

        # Публикуем (один раз в минуту — нормально, т.к. time совпадает только 1 минуту)
        try:
            user = acc.get_user(username=acc.username)
            item_list = user.get_items()
            if not item_list or not getattr(item_list, "items", None):
                continue

            target_ids = autopub.get("item_ids", [])
            published_count = 0

            for item in item_list.items:
                item_id = getattr(item, "id", "")
                # Если указаны конкретные ID — публикуем только их
                if target_ids and item_id not in target_ids:
                    continue

                # Публикуем только неопубликованные
                if getattr(item, "status", "") != "APPROVED":
                    try:
                        statuses = acc.get_item_priority_statuses(item_id, getattr(item, "price", 0))
                        free_status = next((s for s in statuses if getattr(s, "price", -1) == 0), None)
                        if free_status:
                            acc.publish_item(item_id, free_status.id)
                            published_count += 1
                    except Exception:
                        logger.exception("Ошибка публикации %s", item_id)

            if published_count > 0:
                tg.send(f"🔄 Автопубликация: опубликовано <b>{published_count}</b> товар(ов).")
                logger.info("Автопубликация: %s товаров", published_count)

        except Exception:
            logger.exception("Ошибка автопубликации")


# =========================================================================
#  ГЛАВНЫЙ ЦИКЛ
# =========================================================================

def run():
    check_env()

    tg = TelegramNotifier(TG_BOT_TOKEN, TG_CHAT_ID)

    logger.info("Авторизация в Playerok...")
    acc = Account(
        cookies=PLAYEROK_COOKIES,
        user_agent=PLAYEROK_USER_AGENT,
    ).get()
    logger.info("Успешно авторизован как %s (id=%s)", acc.username, acc.id)
    tg.send(f"🤖 Бот запущен. Аккаунт Playerok: <b>{acc.username}</b>\n/help — список команд")

    seen_deal_ids = set(state.get("seen_deal_ids", []))
    seen_review_ids = set(state.get("seen_review_ids", []))
    listener = EventListener(acc)

    # Запускаем фоновый поток для команд Telegram
    def command_loop():
        while True:
            try:
                updates = tg.get_updates()
                for u in updates:
                    msg = u.get("message", {})
                    text = msg.get("text", "")
                    if text:
                        tg.handle_command(acc, text)
            except Exception:
                logger.exception("Ошибка в цикле команд")
            time.sleep(2)

    cmd_thread = threading.Thread(target=command_loop, daemon=True)
    cmd_thread.start()

    # Запускаем фоновый поток для автопубликации
    pub_thread = threading.Thread(target=autopublish_worker, args=(acc, tg), daemon=True)
    pub_thread.start()

    backoff = 5
    while True:
        try:
            for event in listener.listen():
                if event.type is EventTypes.NEW_MESSAGE:
                    handle_new_message(acc, tg, event)

                elif event.type in (EventTypes.NEW_DEAL, EventTypes.ITEM_PAID):
                    handle_sale(acc, tg, event.deal, seen_deal_ids)

                elif event.type == EventTypes.DEAL_HAS_PROBLEM:
                    tg.send(f"❗ Проблема по сделке <code>{event.deal.id}</code>, проверьте Playerok.")

                elif event.type == EventTypes.NEW_REVIEW:
                    handle_new_review(acc, tg, event, seen_review_ids)

            backoff = 5

        except (BotCheckDetectedException, UnauthorizedError) as e:
            logger.error("Проблема с авторизацией: %s", e)
            tg.send(
                "🚫 Бот потерял доступ к аккаунту Playerok. "
                "Обновите PLAYEROK_COOKIES и перезапустите бота."
            )
            time.sleep(60)

        except Exception:
            logger.exception("Слушатель событий упал, переподключаюсь через %s сек.", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    run()
