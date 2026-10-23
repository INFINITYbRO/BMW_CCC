"""
Avito watcher — Telegram-бот, который следит за поисковыми выдачами Авито
и присылает новые объявления.

Команды:
  /add <ссылка> [| название]   — добавить поиск (ссылка из браузера, с фильтрами)
  /list                        — список поисков
  /del <id>                    — удалить поиск
  /price <id> <мин> <макс>     — фильтр по цене (0 = без ограничения)
  /exclude <id> слово, слово   — не присылать объявления с этими словами
  /stats <id>                  — медиана цен по поиску и рекомендуемый торг
  /check                       — проверить все поиски прямо сейчас
"""
import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import statistics
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import ClientSession, web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "180"))  # секунд между циклами
PROXY = os.getenv("PROXY") or None  # http://user:pass@host:port — если Авито режет IP
# Прокси только для Telegram (если api.telegram.org недоступен): socks5://127.0.0.1:10808
TG_PROXY = os.getenv("TG_PROXY") or None
# Или своё зеркало Bot API (например, Cloudflare Worker): https://my-tg.workers.dev
TG_API_URL = os.getenv("TG_API_URL") or None
DB_PATH = os.getenv("DB_PATH", "avito_bot.db")
OFFER_DISCOUNT = float(os.getenv("OFFER_DISCOUNT", "0.12"))  # стартовый торг от цены

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("avito")

# ---------------------------------------------------------------- база

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS searches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        title TEXT,
        min_price INTEGER DEFAULT 0,
        max_price INTEGER DEFAULT 0,
        exclude TEXT DEFAULT '',
        initialized INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS items(
        search_id INTEGER,
        item_id TEXT,
        price INTEGER,
        seen_at INTEGER,
        PRIMARY KEY(search_id, item_id)
    );
    """
)
db.commit()

# SEARCHES в .env / на Render: по одному поиску на строку, формат «ссылка | название».
# Нужны на Render: там диск очищается при каждом перезапуске, и /add-поиски пропадают.
SEARCHES = [l.strip() for l in os.getenv("SEARCHES", "").replace(";;", "\n").splitlines() if l.strip()]
DEFAULT_EXCLUDE = ", ".join(w.strip().lower() for w in os.getenv("DEFAULT_EXCLUDE", "").split(",") if w.strip())


# ---------------------------------------------------------------- парсинг Авито

@dataclass
class Item:
    id: str
    title: str
    price: int
    url: str
    place: str
    date: str
    image: str | None
    description: str


def normalize_url(url: str) -> str:
    """Принудительно сортировка «по дате», чтобы новые были на первой странице."""
    parts = urlsplit(url.strip())
    if "avito.ru" not in parts.netloc:
        raise ValueError("Это не ссылка на Авито")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["s"] = "104"  # сортировка: по дате
    query.pop("p", None)  # первая страница
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, urlencode(query), ""))


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def parse_items(page: str) -> list[Item]:
    soup = BeautifulSoup(page, "lxml")
    result = []
    for card in soup.select('div[data-marker="item"]'):
        item_id = card.get("data-item-id") or card.get("id", "").lstrip("i")
        if not item_id:
            continue
        link = card.select_one('a[data-marker="item-title"]') or card.select_one('a[itemprop="url"]')
        if not link:
            continue
        href = link.get("href", "")
        url = href if href.startswith("http") else "https://www.avito.ru" + href
        title = link.get("title") or _text(link)

        price = 0
        meta = card.select_one('meta[itemprop="price"]')
        if meta and meta.get("content", "").isdigit():
            price = int(meta["content"])
        else:
            digits = re.sub(r"\D", "", _text(card.select_one('[data-marker="item-price"]')))
            price = int(digits) if digits else 0

        place = _text(card.select_one('[data-marker="item-address"]')) or _text(
            card.select_one('[class*="geo-root"]')
        )
        date = _text(card.select_one('[data-marker="item-date"]'))
        img = card.select_one("img")
        image = img.get("src") if img else None
        desc_meta = card.select_one('meta[itemprop="description"]')
        description = desc_meta.get("content", "") if desc_meta else ""

        result.append(Item(item_id, title, price, url, place, date, image, description))
    return result


class Blocked(Exception):
    pass


async def fetch(session: AsyncSession, url: str) -> list[Item]:
    resp = await session.get(url, timeout=30, proxy=PROXY)
    body = resp.text
    if resp.status_code in (403, 429) or "Доступ ограничен" in body or "firewall" in body.lower():
        raise Blocked(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    items = parse_items(body)
    if not items and 'data-marker="item"' not in body and "ничего не найдено" not in body.lower():
        raise RuntimeError("Не нашёл карточек — возможно, Авито поменял вёрстку или показал капчу")
    return items


# ---------------------------------------------------------------- логика цен

def search_prices(search_id: int) -> list[int]:
    rows = db.execute(
        "SELECT price FROM items WHERE search_id=? AND price>0 ORDER BY seen_at DESC LIMIT 200",
        (search_id,),
    ).fetchall()
    return [r[0] for r in rows]


def rub(x: float) -> str:
    return f"{int(x):,}".replace(",", " ") + " ₽"


def round_offer(x: float) -> int:
    return int(round(x / 500.0) * 500)


def price_comment(search_id: int, price: int) -> str:
    if not price:
        return ""
    prices = search_prices(search_id)
    if len(prices) < 5:
        return f"💬 Стартовое предложение: ~{rub(round_offer(price * (1 - OFFER_DISCOUNT)))}"
    med = statistics.median(prices)
    diff = (price - med) / med * 100
    # Если уже дёшево — торгуемся мягко, если дорого — целимся в медиану
    if diff <= -15:
        verdict, target = "🔥 сильно ниже рынка, бери быстро", price * 0.95
    elif diff <= 0:
        verdict, target = "✅ ниже медианы", price * (1 - OFFER_DISCOUNT / 2)
    elif diff <= 20:
        verdict, target = "➖ около рынка", min(price * (1 - OFFER_DISCOUNT), med)
    else:
        verdict, target = "💸 дороже рынка", med * 0.95
    return (
        f"📊 Медиана по поиску: {rub(med)} ({diff:+.0f}%) — {verdict}\n"
        f"💬 Предложить: ~{rub(round_offer(target))}"
    )


# ---------------------------------------------------------------- бот

_session = AiohttpSession(proxy=TG_PROXY) if TG_PROXY else AiohttpSession()
if TG_API_URL:
    _session.api = TelegramAPIServer.from_base(TG_API_URL.rstrip("/"))
bot = Bot(BOT_TOKEN, session=_session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
check_lock = asyncio.Lock()


def allowed(msg: Message) -> bool:
    return not ALLOWED_IDS or msg.from_user.id in ALLOWED_IDS


def owned_search(chat_id: int, sid: str):
    if not sid.isdigit():
        return None
    return db.execute(
        "SELECT * FROM searches WHERE id=? AND chat_id=?", (int(sid), chat_id)
    ).fetchone()


@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    if not allowed(msg):
        return
    await msg.answer(html.escape(__doc__.strip()))


@dp.message(Command("add"))
async def cmd_add(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    if not command.args:
        return await msg.answer("Пришли так: /add https://www.avito.ru/... | Vita Сочи")
    raw, _, title = command.args.partition("|")
    try:
        url = normalize_url(raw)
    except ValueError as e:
        return await msg.answer(str(e))
    cur = db.execute(
        "INSERT INTO searches(chat_id, url, title) VALUES(?,?,?)",
        (msg.chat.id, url, title.strip() or None),
    )
    db.commit()
    await msg.answer(
        f"Добавил поиск #{cur.lastrowid}. Сейчас запомню текущие объявления, "
        "присылать буду только новые."
    )
    asyncio.create_task(run_check())


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not allowed(msg):
        return
    rows = db.execute(
        "SELECT id, url, title, min_price, max_price, exclude FROM searches WHERE chat_id=?",
        (msg.chat.id,),
    ).fetchall()
    if not rows:
        return await msg.answer("Поисков нет. Добавь: /add <ссылка>")
    lines = []
    for sid, url, title, mn, mx, ex in rows:
        name = html.escape(title or "поиск")
        flt = []
        if mn or mx:
            flt.append(f"цена {mn or 0}–{mx or '∞'}")
        if ex:
            flt.append(f"без: {html.escape(ex)}")
        lines.append(f"#{sid} <a href=\"{html.escape(url)}\">{name}</a> {'· ' + '; '.join(flt) if flt else ''}")
    await msg.answer("\n".join(lines), disable_web_page_preview=True)


@dp.message(Command("del"))
async def cmd_del(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    row = owned_search(msg.chat.id, (command.args or "").strip())
    if not row:
        return await msg.answer("Нет такого поиска. /list")
    db.execute("DELETE FROM searches WHERE id=?", (row[0],))
    db.execute("DELETE FROM items WHERE search_id=?", (row[0],))
    db.commit()
    await msg.answer(f"Поиск #{row[0]} удалён.")


@dp.message(Command("price"))
async def cmd_price(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    parts = (command.args or "").split()
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return await msg.answer("Формат: /price <id> <мин> <макс>, например /price 1 5000 15000")
    row = owned_search(msg.chat.id, parts[0])
    if not row:
        return await msg.answer("Нет такого поиска.")
    db.execute("UPDATE searches SET min_price=?, max_price=? WHERE id=?", (int(parts[1]), int(parts[2]), row[0]))
    db.commit()
    await msg.answer("Готово.")


@dp.message(Command("exclude"))
async def cmd_exclude(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    sid, _, words = (command.args or "").partition(" ")
    row = owned_search(msg.chat.id, sid)
    if not row:
        return await msg.answer("Формат: /exclude <id> чехол, картридж, игра")
    clean = ", ".join(w.strip().lower() for w in words.split(",") if w.strip())
    db.execute("UPDATE searches SET exclude=? WHERE id=?", (clean, row[0]))
    db.commit()
    await msg.answer(f"Исключаю: {html.escape(clean) or 'ничего'}")


@dp.message(Command("stats"))
async def cmd_stats(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    row = owned_search(msg.chat.id, (command.args or "").strip())
    if not row:
        return await msg.answer("Формат: /stats <id>")
    prices = sorted(search_prices(row[0]))
    if len(prices) < 3:
        return await msg.answer("Пока мало данных.")
    q = statistics.quantiles(prices, n=4)
    await msg.answer(
        f"Объявлений с ценой: {len(prices)}\n"
        f"Мин: {rub(prices[0])} · 25%: {rub(q[0])}\n"
        f"Медиана: {rub(q[1])} · 75%: {rub(q[2])}\n"
        f"Макс: {rub(prices[-1])}\n\n"
        f"Хорошая покупка — около {rub(round_offer(q[0]))} и ниже."
    )


@dp.message(Command("check"))
async def cmd_check(msg: Message):
    if not allowed(msg):
        return
    await msg.answer("Проверяю…")
    await run_check()
    await msg.answer("Проверка завершена.")


def passes_filters(item: Item, mn: int, mx: int, exclude: str) -> bool:
    if mn and item.price and item.price < mn:
        return False
    if mx and item.price > mx:
        return False
    text = f"{item.title} {item.description}".lower()
    return not any(w and w in text for w in exclude.split(", "))


async def notify(chat_id: int, search_id: int, search_title: str | None, item: Item):
    price = rub(item.price) if item.price else "цена не указана"
    caption = (
        f"🆕 <b>{html.escape(item.title)}</b>\n"
        f"💰 {price}\n"
        f"📍 {html.escape(item.place or '—')} · {html.escape(item.date or '')}\n"
        f"{price_comment(search_id, item.price)}\n"
        f"<a href=\"{html.escape(item.url)}\">Открыть на Авито</a>"
        f"{' · #' + str(search_id) + ' ' + html.escape(search_title) if search_title else ''}"
    )
    try:
        if item.image:
            await bot.send_photo(chat_id, item.image, caption=caption[:1024])
            return
    except Exception:
        pass
    await bot.send_message(chat_id, caption, disable_web_page_preview=False)


blocked_until = 0.0
block_streak = 0


async def run_check():
    global blocked_until, block_streak
    if check_lock.locked():
        return
    async with check_lock:
        if time.time() < blocked_until:
            return
        searches = db.execute(
            "SELECT id, chat_id, url, title, min_price, max_price, exclude, initialized FROM searches"
        ).fetchall()
        async with AsyncSession(impersonate="chrome") as session:
            for sid, chat_id, url, title, mn, mx, exclude, initialized in searches:
                try:
                    items = await fetch(session, url)
                    block_streak = 0
                except Blocked as e:
                    block_streak += 1
                    pause = min(3600, 300 * 2 ** (block_streak - 1))
                    blocked_until = time.time() + pause
                    log.warning("Авито ограничил доступ (%s), пауза %s с", e, pause)
                    if block_streak == 1:
                        await bot.send_message(
                            chat_id,
                            f"⚠️ Авито временно ограничил доступ, повторю через {pause // 60} мин. "
                            "Если повторяется — увеличь CHECK_INTERVAL или задай PROXY.",
                        )
                    return
                except Exception as e:
                    log.exception("Ошибка поиска #%s: %s", sid, e)
                    continue

                now = int(time.time())
                new_items = []
                for it in items:
                    cur = db.execute(
                        "INSERT OR IGNORE INTO items(search_id, item_id, price, seen_at) VALUES(?,?,?,?)",
                        (sid, it.id, it.price, now),
                    )
                    if cur.rowcount:
                        new_items.append(it)
                db.commit()

                if not initialized:
                    db.execute("UPDATE searches SET initialized=1 WHERE id=?", (sid,))
                    db.commit()
                    await bot.send_message(
                        chat_id,
                        f"Поиск #{sid}: запомнил {len(items)} текущих объявлений. "
                        f"Медиану и торг смотри в /stats {sid}",
                    )
                    continue

                for it in reversed(new_items):  # старые — первыми
                    if passes_filters(it, mn, mx, exclude):
                        await notify(chat_id, sid, title, it)
                        await asyncio.sleep(0.5)

                await asyncio.sleep(random.uniform(4, 9))  # пауза между поисками


async def scheduler():
    while True:
        try:
            await run_check()
        except Exception:
            log.exception("Сбой цикла проверки")
        await asyncio.sleep(CHECK_INTERVAL + random.uniform(0, CHECK_INTERVAL * 0.3))


async def wait_for_telegram():
    """Не падаем, если Telegram временно недоступен — ждём и пробуем снова."""
    delay = 5
    while True:
        try:
            me = await bot.get_me()
            log.info("Подключился к Telegram как @%s", me.username)
            return
        except Exception as e:
            log.error(
                "Нет связи с api.telegram.org (%s). Повтор через %s с. "
                "Если не проходит — задай TG_PROXY в .env", e, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


def seed_searches():
    if not SEARCHES or not ALLOWED_IDS:
        return
    owner = next(iter(ALLOWED_IDS))
    for line in SEARCHES:
        raw, _, title = line.partition("|")
        try:
            url = normalize_url(raw)
        except ValueError:
            log.warning("Пропускаю неверную ссылку в SEARCHES: %s", raw)
            continue
        exists = db.execute("SELECT 1 FROM searches WHERE chat_id=? AND url=?", (owner, url)).fetchone()
        if not exists:
            db.execute(
                "INSERT INTO searches(chat_id, url, title, exclude) VALUES(?,?,?,?)",
                (owner, url, title.strip() or None, DEFAULT_EXCLUDE),
            )
            log.info("Добавлен поиск из SEARCHES: %s", title.strip() or url)
    db.commit()


async def start_web():
    """Render (Web Service) требует открытый порт. Заодно — точка для пингов UptimeRobot."""
    port = os.getenv("PORT")
    if not port:
        return
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("HTTP на порту %s", port)


async def keep_awake():
    """Бесплатный Render усыпляет сервис через 15 мин без запросов — пингуем сами себя."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    async with ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                async with s.get(url, timeout=30) as r:
                    log.info("keep-awake: %s", r.status)
            except Exception as e:
                log.warning("keep-awake не удался: %s", e)


async def main():
    await start_web()
    seed_searches()
    asyncio.create_task(keep_awake())
    await wait_for_telegram()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
