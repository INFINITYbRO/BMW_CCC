"""
Avito watcher — Telegram-бот, который следит за поисковыми выдачами Авито
и присылает новые объявления.

Как добавить поиск — просто пришли ссылку с Авито.
Название можно написать с новой строки или через пробел после ссылки:
  https://www.avito.ru/...
  Vita Сочи

Команды (N — номер из /list):
  /list                        — список поисков
  /name N название             — переименовать поиск
  /del N                       — удалить поиск
  /price N мин макс            — фильтр по цене (0 = без ограничения)
  /exclude N слово, слово      — не присылать объявления с этими словами
  /exclude N -                — очистить слова-исключения
  /reserved N on              — скрывать забронированные (по умолчанию)
  /reserved N off             — показывать забронированные
  /stats N                     — медиана цен и рекомендуемый торг
  /check                       — проверить поиски и показать последний результат
  /proxy                       — проверить прокси с сервера бота
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass, field, replace
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
from curl_cffi.requests.exceptions import RequestException
from dotenv import load_dotenv
from avito_parser import (clean_exclusions, is_reserved, normalize_text,
                          parse_details_html, parse_details_markdown)
from proxy_diagnostics import connect_status, probe_target, proxy_conclusion

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # секунд между циклами
PROXY = os.getenv("PROXY") or None  # http://user:pass@host:port — если Авито режет IP
PROXY_FALLBACK = os.getenv("PROXY_FALLBACK", "1").lower() in ("1", "true", "yes")
# Прокси только для Telegram (если api.telegram.org недоступен): socks5://127.0.0.1:10808
TG_PROXY = os.getenv("TG_PROXY") or None
# Или своё зеркало Bot API (например, Cloudflare Worker): https://my-tg.workers.dev
TG_API_URL = os.getenv("TG_API_URL") or None
DB_PATH = os.getenv("DB_PATH", "avito_bot.db")
OFFER_DISCOUNT = float(os.getenv("OFFER_DISCOUNT", "0.12"))  # стартовый торг от цены

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("avito")

# ---------------------------------------------------------------- база

def init_db(connection: sqlite3.Connection):
    """Idempotent migration: preserve existing searches, items and delivery queue."""
    connection.executescript(
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
    CREATE TABLE IF NOT EXISTS pending_notifications(
        search_id INTEGER,
        item_id TEXT,
        payload TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY(search_id, item_id)
    );
    """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(searches)")}
    if "skip_reserved" not in columns:
        connection.execute("ALTER TABLE searches ADD COLUMN skip_reserved INTEGER NOT NULL DEFAULT 1")
    connection.commit()


db = sqlite3.connect(DB_PATH, check_same_thread=False)
init_db(db)

# SEARCHES в .env / на Render: по одному поиску на строку, формат «ссылка | название».
# Нужны на Render: там диск очищается при каждом перезапуске, и /add-поиски пропадают.
SEARCHES = [l.strip() for l in os.getenv("SEARCHES", "").replace(";;", "\n").splitlines() if l.strip()]
DEFAULT_EXCLUDE = clean_exclusions(os.getenv("DEFAULT_EXCLUDE", ""))


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
    reserved: bool = False
    active: bool = True


def normalize_url(url: str) -> str:
    """Принудительно сортировка «по дате», чтобы новые были на первой странице."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if (parts.scheme not in ("http", "https") or parts.username or parts.password
            or parts.port not in (None, 80, 443)
            or not (host == "avito.ru" or host.endswith(".avito.ru"))):
        raise ValueError("Это не ссылка на Авито")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["s"] = "104"  # сортировка: по дате
    query.pop("p", None)  # первая страница
    return urlunsplit(("https", host, parts.path, urlencode(query), ""))


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def parse_items(page: str) -> list[Item]:
    soup = BeautifulSoup(page, "lxml")
    result = []
    for card in soup.select('[data-marker="item"]'):
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
        description = desc_meta.get("content", "") if desc_meta else _text(
            card.select_one('[data-marker="item-description"], [class*="iva-item-description"]'))

        result.append(Item(item_id, title, price, url, place, date, image, description,
                           is_reserved(card.get_text("\n", strip=True))))
    return result


class FetchError(Exception):
    """Contains only a safe, user-facing diagnostic, never a raw request URL."""


class Blocked(FetchError):
    pass


proxy_retry_at = 0.0
connection_note = ""


def network_error(exc: RequestException, via_proxy: bool) -> str:
    message = str(exc).lower()
    status = connect_status(exc) if via_proxy else None
    if status == 403:
        return ("Прокси запретил HTTPS-туннель (CONNECT HTTP 403). "
                "Причина запрета не указана. Команда /proxy проверит доступ с сервера бота.")
    if status == 407:
        return "Прокси требует авторизацию (CONNECT HTTP 407). Проверь логин и пароль PROXY в Render."
    if via_proxy and ("connect tunnel failed" in message or "proxy" in message
                      or int(exc.code) in (5, 7, 28, 97)):
        status = re.search(r"response\s+(\d{3})", message)
        detail = f" (HTTP {status[1]})" if status else ""
        return (
            f"Прокси отклонил соединение или недоступен{detail}. "
            "Проверь оплату, доступ по IP, адрес и пароль PROXY на Render."
        )
    return "Не удалось подключиться к Авито. Проверь сеть и настройку PROXY на Render."


async def fetch_direct_page(session: AsyncSession, url: str, allow_missing: bool = False) -> tuple[str, int]:
    global proxy_retry_at, connection_note
    via_proxy = bool(PROXY) and time.time() >= proxy_retry_at
    options = {"proxy": PROXY} if via_proxy else {"proxies": {"all": ""}}
    try:
        resp = await session.get(url, timeout=30, **options)
    except RequestException as exc:
        reason = network_error(exc, via_proxy)
        if not (via_proxy and PROXY_FALLBACK):
            raise FetchError(reason) from None
        # An unavailable proxy must not silently disable every search.
        proxy_retry_at = time.time() + 300
        connection_note = reason + " Пробую прямое соединение."
        log.warning("%s", connection_note)
        try:
            via_proxy = False
            resp = await session.get(url, timeout=30, proxies={"all": ""})
        except RequestException:
            raise FetchError(reason + " Прямое соединение тоже недоступно.") from None
    if via_proxy and resp.status_code != 407:
        # A response from the HTTPS target proves that the tunnel now works.
        # Do not keep showing an old CONNECT failure after proxy recovery.
        connection_note = ""
        proxy_retry_at = 0.0
    body = resp.text
    if allow_missing and resp.status_code in (404, 410):
        return body, resp.status_code
    # Avito's normal HTML includes CAPTCHA/firewall strings inside JavaScript.
    # Diagnose the rendered page text, not dormant scripts on a valid listing.
    soup = BeautifulSoup(body, "lxml")
    for node in soup.select("script, style, template, noscript"):
        node.decompose()
    visible = soup.get_text(" ", strip=True).lower()
    has_content = bool(soup.select_one(
        '[data-marker="item-view/item-description"], '
        '[data-marker="item"] a[data-marker="item-title"], '
        '[data-marker="item"] a[itemprop="url"]'))
    if (resp.status_code in (401, 403, 407, 429, 439)
            or (not has_content and any(marker in visible for marker in (
                "доступ ограничен", "подтвердите, что вы не робот", "firewall",
                "challenge-platform", "проверка браузера")))):
        route = "через прокси" if via_proxy else "напрямую"
        raise Blocked(
            f"Авито ограничил доступ {route} (HTTP {resp.status_code}). "
            "Нужен рабочий прокси с доступом к Авито."
        )
    if resp.status_code >= 400:
        raise FetchError(f"Авито вернул HTTP {resp.status_code}. Проверь ссылку поиска.")
    return body, resp.status_code


async def fetch_direct(session: AsyncSession, url: str) -> list[Item]:
    body, _ = await fetch_direct_page(session, url)
    lowered = body.lower()
    items = parse_items(body)
    if not items and not any(text in lowered for text in (
            "ничего не найдено", "ничего не нашлось", "нет объявлений",
            'data-marker="search-results/empty"')):
        raise FetchError(
            "Не удалось прочитать объявления: изменилась страница Авито или показана капча."
        )
    return items


# ---------------------------------------------------------------- free cloud reader

FETCH_MODE = os.getenv("AVITO_FETCH_MODE", "reader").lower()
if FETCH_MODE not in ("reader", "direct"):
    raise ValueError("AVITO_FETCH_MODE must be reader or direct")
if FETCH_MODE == "reader":
    CHECK_INTERVAL = max(CHECK_INTERVAL, 300)


READER_REFRESH_INTERVAL = max(300, CHECK_INTERVAL)


@dataclass
class ReaderSnapshot:
    started_at: float
    items: list[Item] | None
    error: str = ""
    last_success_at: float | None = None
    last_success_count: int = 0


reader_snapshots: dict[str, ReaderSnapshot] = {}


def recent_reader_snapshot(url: str) -> ReaderSnapshot | None:
    snapshot = reader_snapshots.get(normalize_url(url))
    if snapshot and time.monotonic() - snapshot.started_at < READER_REFRESH_INTERVAL:
        return snapshot
    return None


def reader_snapshot_note(sid: int, owner: int, snapshot: ReaderSnapshot) -> str:
    now = time.monotonic()
    remaining = max(1, READER_REFRESH_INTERVAL - int(now - snapshot.started_at))
    note = f"Поиск #{search_num(owner, sid)}: новый запрос к источнику через {remaining} с."
    if snapshot.last_success_at is not None:
        age = max(0, int(now - snapshot.last_success_at))
        note += (
            f" Последняя успешная проверка — {age} с назад, "
            f"объявлений: {snapshot.last_success_count}."
        )
    return note


def parse_reader_items(markdown: str) -> list[Item]:
    """Read listing cards from Reader's deterministic Markdown, without an LLM."""
    matches = list(re.finditer(
        r"\]\((https?://(?:www\.)?avito\.ru/[^\s)]*_\d{8,}(?:\?[^\s)]*)?)\)",
        markdown,
    ))
    # Image, title and «ещё фото» can all link to the SAME item. Use the next
    # different ID as a boundary, otherwise the price/description get lost.
    groups = []
    for match in matches:
        parts = urlsplit(html.unescape(match[1]))
        item_id = re.search(r"_(\d{8,})$", parts.path)[1]
        if groups and groups[-1][0] == item_id:
            groups[-1][2].append(match)
        else:
            groups.append((item_id, parts, [match]))
    result: list[Item] = []
    seen: set[str] = set()
    for index, (item_id, parts, links) in enumerate(groups):
        if item_id in seen:
            continue
        match = links[0]
        start = markdown.rfind("\n", 0, match.start()) + 1
        label = markdown[start:match.start()]
        end = (markdown.rfind("\n", 0, groups[index + 1][2][0].start()) + 1
               if index + 1 < len(groups) else len(markdown))
        details = markdown[match.end():end]
        image_match = re.search(r"!\[([^\]]+)\]\((https?://[^\s)]+)\)", label)
        title = ""
        place = ""
        image = None
        if image_match:
            alt = re.sub(r"^Image\s+\d+:\s*", "", image_match[1])
            title, separator, place = alt.rpartition(", ")
            if not separator:
                title, place = alt, ""
            image = html.unescape(image_match[2])
        else:
            title = re.sub(r"^[\s\[*#]+", "", label).strip(" ]*")
        if not title or re.fullmatch(r"\*?\s*Ещё \d+ фото", title):
            title = parts.path.rsplit("/", 1)[-1].rsplit("_", 1)[0].replace("_", " ")
        price_match = re.search(r"(\d[\d\s\u00a0\u202f]*)\s*(?:₽|руб\.?)", details)
        price = int(re.sub(r"\D", "", price_match[1])) if price_match else 0
        date_match = re.search(
            r"(?im)^\s*(сегодня[^\n]*|вчера[^\n]*|\d+\s+(?:минут[уы]?|час[аов]*|дн[яей]*)\s+назад)\s*$",
            details,
        )
        seen.add(item_id)
        result.append(Item(
            item_id, html.unescape(title), price,
            urlunsplit(("https", "www.avito.ru", parts.path, "", "")),
            html.unescape(place), date_match[1].strip() if date_match else "",
            image, re.sub(r"[\*_#]", "", details).strip(),
            is_reserved(details),
        ))
    return result


reader_request_at = 0.0


async def pace_reader():
    """Share the unauthenticated Reader request budget across search and details."""
    global reader_request_at
    delay = reader_request_at - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)
    reader_request_at = time.monotonic() + 3.2


async def fetch_reader(session: AsyncSession, url: str) -> list[Item]:
    # Only the public search URL is sent. Never forward Telegram credentials,
    # the configured proxy, or an Avito user's cookies to the reader service.
    target = normalize_url(url)
    redirects = (301, 302, 303, 307, 308)
    for attempt in range(2):
        headers = {
            "Accept": "application/json", "X-Cache-Tolerance": "0", "X-Timeout": "45",
        }
        if attempt:
            # A redirect or partial document may need ordinary browser rendering.
            headers.update({"X-Engine": "browser", "X-Respond-Timing": "resource-idle"})
        try:
            await pace_reader()
            response = await session.get(
                "https://r.jina.ai/" + target, headers=headers,
                timeout=65, proxies={"all": ""}, allow_redirects=True, max_redirects=5,
            )
        except RequestException:
            raise FetchError("Бесплатный сервис чтения временно недоступен. Повторю позже.") from None
        if response.status_code == 429:
            raise FetchError("Бесплатный сервис чтения ограничил частоту запросов. Повторю позже.")
        if response.status_code in redirects and not attempt:
            await asyncio.sleep(2)
            continue
        if response.status_code != 200:
            raise FetchError(f"Сервис чтения вернул HTTP {response.status_code}. Повторю позже.")
        try:
            payload = response.json()
            data = payload["data"]
            if not isinstance(data, dict) or not isinstance(data.get("content", ""), str):
                raise ValueError("Invalid reader response")
            if normalize_url(data["url"]) != target:
                raise ValueError("Reader returned another search")
            status = data["httpStatus"]
            if type(status) is not int:
                raise ValueError("Invalid target HTTP status")
            content = data.get("content", "")
        except (ValueError, TypeError, KeyError):
            raise FetchError("Не удалось прочитать ответ бесплатного сервиса.") from None

        if status in (403, 429, 439):
            raise FetchError(
                f"Авито временно ограничил доступ сервису чтения (HTTP {status}). "
                "Автопроверки продолжаются; повторю позже."
            )
        if status != 200 and status not in redirects:
            raise FetchError(
                f"Авито отклонил запрос сервиса чтения (HTTP {status}). Повторю позже."
            )
        items = parse_reader_items(content)
        if status in redirects:
            # Reader can report an intermediate redirect while already returning
            # the final listing content. Require actual priced cards as evidence.
            if items and any(item.price > 0 for item in items):
                log.info("Reader: HTTP %s после перенаправления, прочитано %s объявлений",
                         status, len(items))
                return items
            if not attempt:
                await asyncio.sleep(2)
                continue
            raise FetchError(
                f"Авито перенаправил запрос (HTTP {status}), "
                "но страницу объявлений прочитать не удалось. Повторю позже."
            )
        lowered = content.lower()
        if items or any(text in lowered for text in (
                "ничего не найдено", "ничего не нашлось", "нет объявлений")):
            return items
        if not attempt:
            await asyncio.sleep(2)
            continue
        raise FetchError("Сервис чтения не вернул карточки объявлений. Повторю позже.")
    raise FetchError("Не удалось завершить переход к странице объявлений. Повторю позже.")


async def fetch(session: AsyncSession, url: str) -> list[Item]:
    if FETCH_MODE == "reader":
        target = normalize_url(url)
        snapshot = recent_reader_snapshot(target)
        if snapshot:
            if snapshot.error:
                raise FetchError(snapshot.error)
            return list(snapshot.items or [])
        previous = reader_snapshots.get(target)
        started = time.monotonic()
        try:
            try:
                items = await fetch_reader(session, target)
            except FetchError as reader_error:
                # A Reader IP can be blocked even when the deployment's own IP
                # works. Try direct access, honouring the existing PROXY options.
                try:
                    items = await fetch_direct(session, target)
                except FetchError as direct_error:
                    raise FetchError(f"{reader_error} Резервный доступ: {direct_error}") from None
        except FetchError as exc:
            # Also throttle failed attempts. Repeated /check must not hammer a
            # blocked source or replace its last successful data with zero ads.
            reader_snapshots[target] = ReaderSnapshot(
                started, None, str(exc),
                previous.last_success_at if previous else None,
                previous.last_success_count if previous else 0,
            )
            raise
        reader_snapshots[target] = ReaderSnapshot(
            started, items, last_success_at=time.monotonic(), last_success_count=len(items),
        )
        return items
    return await fetch_direct(session, url)


@dataclass
class DetailSnapshot:
    started_at: float
    item: Item | None = None
    error: str = ""


detail_snapshots: dict[str, DetailSnapshot] = {}


def detail_refresh_interval() -> int:
    return max(300, CHECK_INTERVAL) if FETCH_MODE == "reader" else max(30, min(60, CHECK_INTERVAL))


def remember_details(target: str, snapshot: DetailSnapshot):
    if target not in detail_snapshots and len(detail_snapshots) >= 500:
        detail_snapshots.pop(next(iter(detail_snapshots)))
    detail_snapshots[target] = snapshot


def listing_url(url: str) -> str:
    parts = urlsplit(normalize_url(url))  # validate the host, credentials and port
    return urlunsplit(("https", "www.avito.ru", parts.path, "", ""))


async def read_item_details(session: AsyncSession, item: Item) -> Item:
    target = listing_url(item.url)
    if FETCH_MODE == "direct":
        page, status = await fetch_direct_page(session, target, allow_missing=True)
        if status in (404, 410):
            return replace(item, active=False)
        try:
            details = parse_details_html(page, item.id)
        except ValueError:
            raise FetchError("Не удалось прочитать полное описание и статус объявления.") from None
    else:
        await pace_reader()
        try:
            response = await session.get(
                "https://r.jina.ai/" + target,
                headers={"Accept": "application/json", "X-Cache-Tolerance": "0",
                         "X-Timeout": "45", "X-Respond-With": "html"},
                timeout=65, proxies={"all": ""}, allow_redirects=True, max_redirects=5,
            )
        except RequestException:
            raise FetchError("Сервис чтения карточек временно недоступен.") from None
        if response.status_code != 200:
            raise FetchError(f"Сервис чтения карточек вернул HTTP {response.status_code}.")
        try:
            data = response.json()["data"]
            if not isinstance(data, dict) or not isinstance(data.get("url"), str):
                raise ValueError("Invalid reader data")
            if listing_url(data["url"]) != target or type(data["httpStatus"]) is not int:
                raise ValueError("Another listing or invalid target status")
            status = data["httpStatus"]
            page = data.get("html") or data.get("content", "")
            if not isinstance(page, str):
                raise ValueError("Invalid page")
        except (KeyError, ValueError, TypeError):
            raise FetchError("Некорректный ответ сервиса чтения карточек.") from None
        if status in (404, 410):
            return replace(item, active=False)
        if status not in (200, 301, 302, 303, 307, 308):
            raise FetchError(f"Авито не дал проверить карточку (HTTP {status}).")
        try:
            details = (parse_details_html(page, item.id) if "<" in page
                       else parse_details_markdown(page))
        except ValueError:
            raise FetchError("Не удалось прочитать полное описание и статус объявления.") from None
    return replace(item, description=details.description, reserved=details.reserved, active=details.active)


async def fetch_details(session: AsyncSession, item: Item) -> Item:
    """Cache successes AND failures so repeated /check cannot flood the source."""
    target = listing_url(item.url)
    interval = detail_refresh_interval()
    snapshot = detail_snapshots.get(target)
    if snapshot and time.monotonic() - snapshot.started_at < interval:
        if snapshot.error:
            raise FetchError(snapshot.error)
        return replace(item, description=snapshot.item.description,
                       reserved=snapshot.item.reserved, active=snapshot.item.active)
    started = time.monotonic()
    try:
        try:
            detailed = await read_item_details(session, item)
        except FetchError as reader_error:
            if FETCH_MODE != "reader":
                raise
            try:
                page, status = await fetch_direct_page(session, target, allow_missing=True)
                if status in (404, 410):
                    detailed = replace(item, active=False)
                else:
                    try:
                        details = parse_details_html(page, item.id)
                    except ValueError:
                        raise FetchError("Не удалось проверить описание и бронь через резервный доступ.") from None
                    detailed = replace(item, description=details.description,
                                       reserved=details.reserved, active=details.active)
            except FetchError as direct_error:
                raise FetchError(f"{reader_error} Резервный доступ: {direct_error}") from None
    except FetchError as exc:
        remember_details(target, DetailSnapshot(started, error=str(exc)))
        raise
    # Bound memory independently from the persistent queue / seen history.
    remember_details(target, DetailSnapshot(started, detailed))
    return detailed


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
        return f"<tg-emoji emoji-id='5870753782874246579'>✍</tg-emoji> Стартовое предложение: ~{rub(round_offer(price * (1 - OFFER_DISCOUNT)))}"
    med = statistics.median(prices)
    diff = (price - med) / med * 100
    # Если уже дёшево — торгуемся мягко, если дорого — целимся в медиану
    if diff <= -15:
        verdict, target = "<tg-emoji emoji-id='5870930636742595124'>📊</tg-emoji> сильно ниже рынка, бери быстро", price * 0.95
    elif diff <= 0:
        verdict, target = "<tg-emoji emoji-id='5870633910337015697'>✅</tg-emoji> ниже медианы", price * (1 - OFFER_DISCOUNT / 2)
    elif diff <= 20:
        verdict, target = "<tg-emoji emoji-id='6028435952299413210'>ℹ</tg-emoji> около рынка", min(price * (1 - OFFER_DISCOUNT), med)
    else:
        verdict, target = "<tg-emoji emoji-id='5904462880941545555'>🪙</tg-emoji> дороже рынка", med * 0.95
    return (
        f"<tg-emoji emoji-id='5870921681735781843'>📊</tg-emoji> Медиана по поиску: {rub(med)} ({diff:+.0f}%) — {verdict}\n"
        f"<tg-emoji emoji-id='5870753782874246579'>✍</tg-emoji> Предложить: ~{rub(round_offer(target))}"
    )


# ---------------------------------------------------------------- бот

_session = AiohttpSession(proxy=TG_PROXY) if TG_PROXY else AiohttpSession()
if TG_API_URL:
    _session.api = TelegramAPIServer.from_base(TG_API_URL.rstrip("/"))
bot = Bot(BOT_TOKEN, session=_session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
check_lock = asyncio.Lock()


def allowed(msg: Message) -> bool:
    return not ALLOWED_IDS or bool(msg.from_user and msg.from_user.id in ALLOWED_IDS)


def chat_search_ids(chat_id: int) -> list[int]:
    return [r[0] for r in db.execute("SELECT id FROM searches WHERE chat_id=? ORDER BY id", (chat_id,))]


def search_num(chat_id: int, sid: int) -> int:
    """Номер поиска, который видит пользователь: 1, 2, 3… без дыр после удаления."""
    ids = chat_search_ids(chat_id)
    return ids.index(sid) + 1 if sid in ids else 0


def owned_search(chat_id: int, num: str):
    num = num.strip().lstrip("#")
    if not num.isdigit():
        return None
    ids = chat_search_ids(chat_id)
    n = int(num)
    if not 1 <= n <= len(ids):
        return None
    return db.execute("SELECT * FROM searches WHERE id=?", (ids[n - 1],)).fetchone()


def default_title(url: str) -> str:
    """Название по умолчанию — текст поиска из ссылки (q=ps+vita → «ps vita»)."""
    q = dict(parse_qsl(urlsplit(url).query)).get("q", "").strip()
    return q or "поиск"


def split_link_and_title(text: str) -> tuple[str, str]:
    """«ссылка | название», «ссылка название» или ссылка и название с новой строки."""
    text = text.strip()
    m = re.search(r"https?://\S*avito\.ru\S*", text)
    if not m:
        return "", ""
    rest = (text[: m.start()] + " " + text[m.end():]).replace("|", " ")
    return m.group(0), " ".join(rest.split())


@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    if not allowed(msg):
        return
    await msg.answer(html.escape(__doc__.strip()))


async def add_search(msg: Message, text: str):
    raw, title = split_link_and_title(text)
    if not raw:
        return await msg.answer(
            "Пришли ссылку с Авито, а с новой строки — название, например:\n"
            "https://www.avito.ru/sochi/...?q=ps+vita\nVita Сочи"
        )
    try:
        url = normalize_url(raw)
    except ValueError as e:
        return await msg.answer(str(e))
    if db.execute("SELECT 1 FROM searches WHERE chat_id=? AND url=?", (msg.chat.id, url)).fetchone():
        return await msg.answer("Этот поиск уже есть. /list")
    title = title or default_title(url)
    cur = db.execute(
        "INSERT INTO searches(chat_id, url, title, exclude) VALUES(?,?,?,?)",
        (msg.chat.id, url, title, DEFAULT_EXCLUDE),
    )
    db.commit()
    n = search_num(msg.chat.id, cur.lastrowid)
    await msg.answer(
        f"Добавил поиск #{n} «{html.escape(title)}». Сейчас запомню текущие объявления, "
        f"присылать буду только новые.\nПереименовать: /name {n} новое название"
    )
    asyncio.create_task(run_check(chat_id=msg.chat.id))


@dp.message(Command("add"))
async def cmd_add(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    await add_search(msg, command.args or "")


@dp.message(Command("name"))
async def cmd_name(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    num, _, title = (command.args or "").strip().partition(" ")
    row = owned_search(msg.chat.id, num)
    if not row or not title.strip():
        return await msg.answer("Формат: /name 1 Vita Сочи")
    db.execute("UPDATE searches SET title=? WHERE id=?", (title.strip(), row[0]))
    db.commit()
    await msg.answer(f"Поиск #{num.lstrip('#')} теперь называется «{html.escape(title.strip())}».")


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not allowed(msg):
        return
    rows = db.execute(
        "SELECT id, url, title, min_price, max_price, exclude, skip_reserved FROM searches WHERE chat_id=? ORDER BY id",
        (msg.chat.id,),
    ).fetchall()
    if not rows:
        return await msg.answer("Поисков нет. Пришли ссылку с Авито, чтобы добавить.")
    lines = []
    for n, (sid, url, title, mn, mx, ex, skip_reserved) in enumerate(rows, 1):
        name = html.escape(title or "поиск")
        flt = []
        if mn or mx:
            flt.append(f"цена {mn or 0}–{mx or '∞'}")
        if ex:
            flt.append(f"без: {html.escape(ex)}")
        flt.append("без брони" if skip_reserved else "включая бронь")
        lines.append(f"#{n} <a href=\"{html.escape(url)}\">{name}</a> {'· ' + '; '.join(flt) if flt else ''}")
    await msg.answer("\n".join(lines), disable_web_page_preview=True)


@dp.message(Command("del"))
async def cmd_del(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    num = (command.args or "").strip()
    row = owned_search(msg.chat.id, num)
    if not row:
        return await msg.answer("Нет такого поиска. /list")
    db.execute("DELETE FROM searches WHERE id=?", (row[0],))
    db.execute("DELETE FROM items WHERE search_id=?", (row[0],))
    db.execute("DELETE FROM pending_notifications WHERE search_id=?", (row[0],))
    db.commit()
    await msg.answer(f"Поиск «{html.escape(row[3] or 'поиск')}» удалён. Номера в /list обновились.")


@dp.message(Command("price"))
async def cmd_price(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    parts = (command.args or "").split()
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return await msg.answer("Формат: /price N мин макс, например /price 1 5000 15000")
    row = owned_search(msg.chat.id, parts[0])
    if not row:
        return await msg.answer("Нет такого поиска.")
    if int(parts[2]) and int(parts[1]) > int(parts[2]):
        return await msg.answer("Минимальная цена не может быть больше максимальной.")
    db.execute("UPDATE searches SET min_price=?, max_price=? WHERE id=?", (int(parts[1]), int(parts[2]), row[0]))
    db.commit()
    await msg.answer("Готово.")


@dp.message(Command("exclude"))
async def cmd_exclude(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    parts = (command.args or "").strip().split(maxsplit=1)
    sid = parts[0] if parts else ""
    words = parts[1] if len(parts) > 1 else ""
    row = owned_search(msg.chat.id, sid)
    if not row:
        return await msg.answer("Формат: /exclude 1 чехол, картридж, игра")
    clean = clean_exclusions(words)
    db.execute("UPDATE searches SET exclude=? WHERE id=?", (clean, row[0]))
    db.commit()
    await msg.answer(f"Исключаю из названия и полного описания: {html.escape(clean) or 'ничего'}")


@dp.message(Command("reserved"))
async def cmd_reserved(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    parts = (command.args or "").strip().split()
    switches = {"on": 1, "1": 1, "вкл": 1, "off": 0, "0": 0, "выкл": 0}
    if len(parts) != 2 or parts[1].lower() not in switches:
        return await msg.answer("Формат: /reserved 1 on — скрывать бронь, /reserved 1 off — показывать.")
    row = owned_search(msg.chat.id, parts[0])
    if not row:
        return await msg.answer("Нет такого поиска. /list")
    enabled = switches[parts[1].lower()]
    db.execute("UPDATE searches SET skip_reserved=? WHERE id=?", (enabled, row[0]))
    db.commit()
    await msg.answer("Забронированные объявления скрыты." if enabled else
                     "Забронированные объявления тоже будут приходить.")


@dp.message(Command("stats"))
async def cmd_stats(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    row = owned_search(msg.chat.id, (command.args or "").strip())
    if not row:
        return await msg.answer("Формат: /stats 1")
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
    report = await run_check(chat_id=msg.chat.id)
    await msg.answer(report.text())


proxy_probe_lock = asyncio.Lock()
proxy_probe_reports: dict[int, tuple[float, str]] = {}


async def build_proxy_report(chat_id: int) -> str:
    row = db.execute("SELECT url FROM searches WHERE chat_id=? ORDER BY id LIMIT 1", (chat_id,)).fetchone()
    target = normalize_url(row[0]) if row else "https://www.avito.ru/"
    lines = ["Проверка PROXY с сервера бота. Все запросы идут только через настроенный прокси."]
    async with AsyncSession(impersonate="chrome", trust_env=False) as session:
        control = await probe_target(session, PROXY, "https://github.com/robots.txt")
        avito = await probe_target(session, PROXY, target)
        lines.extend((control.line("Контрольный HTTPS-сайт"), avito.line("Авито")))
        if avito.http_status == 200 and row:
            items = parse_items(avito.body)
            lines.append(f"Карточек в выдаче: {len(items)}.")
            if items:
                item = items[0]
                detail = await probe_target(session, PROXY, listing_url(item.url))
                lines.append(detail.line("Полное объявление"))
                if detail.http_status == 200:
                    try:
                        parsed = parse_details_html(detail.body, item.id)
                        lines.append(f"Полное описание распознано: {len(parsed.description)} символов. "
                                     f"Статус брони: {'есть' if parsed.reserved else 'нет'}.")
                    except ValueError:
                        lines.append("Полное описание и статус распознать не удалось; фильтры не подтверждены.")
            else:
                lines.append("Ответ HTTP 200 сам по себе не подтверждает чтение объявлений.")
        elif not row:
            lines.append("Чтобы проверить выдачу и описание, добавь свою ссылку поиска.")
        lines.append(proxy_conclusion(control, avito))
    return "\n".join(lines)


@dp.message(Command("proxy"))
async def cmd_proxy(msg: Message):
    if not allowed(msg):
        return
    if not PROXY:
        return await msg.answer("PROXY не задан в окружении сервера бота.")
    previous = proxy_probe_reports.get(msg.chat.id)
    if previous and time.monotonic() - previous[0] < 300:
        remaining = max(1, 300 - int(time.monotonic() - previous[0]))
        return await msg.answer(html.escape(f"Последняя проверка прокси; новый запрос через {remaining} с.\n{previous[1]}"))
    if proxy_probe_lock.locked():
        return await msg.answer("Проверка прокси уже идёт. Повтори /proxy чуть позже.")
    async with proxy_probe_lock:
        await msg.answer("Проверяю прокси с сервера бота… Это займёт до 45 секунд.")
        try:
            report = await build_proxy_report(msg.chat.id)
        except Exception:
            # Transport exceptions can contain credentials, so never interpolate
            # or log them, even when an unexpected diagnostic failure occurs.
            report = "Не удалось завершить диагностику прокси. Повтори позже."
        proxy_probe_reports[msg.chat.id] = (time.monotonic(), report)
        await msg.answer(html.escape(report))


@dp.message()
async def any_message(msg: Message):
    if not allowed(msg) or not msg.text:
        return
    if "avito.ru" in msg.text:
        return await add_search(msg, msg.text)
    await msg.answer("Пришли ссылку с Авито (можно с названием) или /help")


def passes_filters(item: Item, mn: int, mx: int, exclude: str, skip_reserved: bool = True) -> bool:
    if not item.active:
        return False
    if skip_reserved and (item.reserved or is_reserved(item.title + "\n" + item.description)):
        return False
    if mn and item.price and item.price < mn:
        return False
    if mx and item.price > mx:
        return False
    text = normalize_text(f"{item.title} {item.description}")
    return not any(w and w in text for w in clean_exclusions(exclude).split(", "))


async def notify(chat_id: int, search_id: int, search_title: str | None, item: Item):
    price = rub(item.price) if item.price else "цена не указана"
    caption = (
        f"<tg-emoji emoji-id='5870528606328852614'>📁</tg-emoji> <b>{html.escape(item.title[:180])}</b>\n"
        f"<tg-emoji emoji-id='5904462880941545555'>🪙</tg-emoji> {price}\n"
        f"<tg-emoji emoji-id='6042011682497106307'>📍</tg-emoji> {html.escape((item.place or '—')[:100])} · {html.escape((item.date or '')[:80])}\n"
        f"{price_comment(search_id, item.price)}\n"
        f"<a href=\"{html.escape(item.url)}\">Открыть на Авито</a>"
        f" · #{search_num(chat_id, search_id)} {html.escape((search_title or '')[:100])}"
    )
    try:
        if item.image:
            if len(html.unescape(re.sub(r"<[^>]*>", "", caption))) > 1024:
                await bot.send_message(chat_id, caption, disable_web_page_preview=False)
                return
            await bot.send_photo(chat_id, item.image, caption=caption)
            return
    except Exception:
        pass
    await bot.send_message(chat_id, caption, disable_web_page_preview=False)


@dataclass
class CheckReport:
    checked: int = 0
    found: int = 0
    sent: int = 0
    filtered: int = 0
    deferred: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: str = ""

    def text(self) -> str:
        if self.skipped:
            return self.skipped
        lines = [
            f"Проверено поисков: {self.checked}. Объявлений на страницах: {self.found}.",
            f"Отправлено: {self.sent}. Новых исключено фильтрами: {self.filtered}.",
        ]
        if self.deferred:
            lines.append(f"В очереди на проверку или отправку: {self.deferred}. Повторю автоматически.")
        if connection_note:
            lines.append(connection_note)
        lines.extend(self.errors)
        lines.extend(self.notes)
        if not self.checked and not self.errors:
            lines.append("Поисков нет. Пришли ссылку с Авито.")
        return html.escape("\n".join(lines))


blocked_until = 0.0
block_streak = 0
error_notices: dict[int | tuple[int, str], tuple[str, float]] = {}


async def report_error(chat_id: int, sid: int, reason: str, scope: str = "search"):
    key = sid if scope == "search" else (sid, scope)
    previous, when = error_notices.get(key, ("", 0.0))
    if reason == previous and time.time() - when < 3600:
        return
    try:
        await bot.send_message(
            chat_id, html.escape(f"Поиск #{search_num(chat_id, sid)}: {reason}")
        )
        error_notices[key] = (reason, time.time())
    except Exception:
        log.warning("Не удалось сообщить об ошибке поиска #%s", sid)


@dataclass
class DeliveryContext:
    attempted: set[tuple[int, str]] = field(default_factory=set)
    detail_requests: int = 0
    detail_errors: set[int] = field(default_factory=set)


def delivery_settings(sid: int, chat_id: int):
    return db.execute(
        "SELECT title, min_price, max_price, exclude, skip_reserved FROM searches WHERE id=? AND chat_id=?",
        (sid, chat_id),
    ).fetchone()


async def deliver_pending(session: AsyncSession, sid: int, chat_id: int,
                          report: CheckReport, context: DeliveryContext) -> bool:
    rows = db.execute(
        "SELECT item_id, payload FROM pending_notifications WHERE search_id=? "
        "ORDER BY created_at, rowid", (sid,),
    ).fetchall()
    for item_id, payload in rows:
        if (sid, item_id) in context.attempted:
            continue
        settings = delivery_settings(sid, chat_id)
        if settings is None:  # /del may run while a request is in progress
            return True
        title, mn, mx, exclude, skip_reserved = settings
        item = Item(**json.loads(payload))
        if passes_filters(item, mn, mx, exclude, skip_reserved) and (exclude or skip_reserved):
            snapshot = detail_snapshots.get(listing_url(item.url))
            cached_detail = snapshot and time.monotonic() - snapshot.started_at < detail_refresh_interval()
            if not cached_detail and context.detail_requests >= 10:
                continue
            context.attempted.add((sid, item_id))
            if not cached_detail:
                context.detail_requests += 1
            try:
                item = await fetch_details(session, item)
            except FetchError as exc:
                # Leave it in the queue even when it falls off the search page.
                reason = f"Проверка описания и брони отложена: {exc} Объявления сохранены в очереди."
                if sid not in context.detail_errors:
                    context.detail_errors.add(sid)
                    report.errors.append(f"Поиск #{search_num(chat_id, sid)}: {reason}")
                    await report_error(chat_id, sid, reason, scope="details")
                continue
            settings = delivery_settings(sid, chat_id)
            if settings is None:
                return True
            title, mn, mx, exclude, skip_reserved = settings
            db.execute("UPDATE pending_notifications SET payload=? WHERE search_id=? AND item_id=?",
                       (json.dumps(asdict(item), ensure_ascii=False), sid, item_id))
            db.commit()
        context.attempted.add((sid, item_id))
        if passes_filters(item, mn, mx, exclude, skip_reserved):
            try:
                await notify(chat_id, sid, title, item)
            except Exception:
                # Keep the payload even when the listing disappears from the next page.
                log.warning("Ошибка доставки объявления для поиска #%s; сохранено для повтора", sid)
                report.errors.append(
                    f"Поиск #{search_num(chat_id, sid)}: не удалось отправить объявление "
                    "в Telegram. Оно сохранено для повторной отправки."
                )
                return False
            report.sent += 1
            await asyncio.sleep(0.5)
        else:
            report.filtered += 1
        db.execute(
            "DELETE FROM pending_notifications WHERE search_id=? AND item_id=?", (sid, item_id)
        )
        db.commit()
    return True


async def run_check(chat_id: int | None = None) -> CheckReport:
    global blocked_until, block_streak
    report = CheckReport()
    if check_lock.locked():
        report.skipped = "Проверка уже идёт. Попробуй /check чуть позже."
        return report
    async with check_lock:
        context = DeliveryContext()
        query = (
            "SELECT id, chat_id, url, title, min_price, max_price, exclude, initialized, skip_reserved "
            "FROM searches"
        )
        searches = db.execute(
            query + (" WHERE chat_id=?" if chat_id is not None else ""),
            (chat_id,) if chat_id is not None else (),
        ).fetchall()
        async with AsyncSession(impersonate="chrome", trust_env=False) as session:
            for sid, owner, url, title, mn, mx, exclude, initialized, skip_reserved in searches:
                # Retry Telegram independently of Avito availability and its backoff.
                delivery_ok = await deliver_pending(session, sid, owner, report, context)
                if time.time() < blocked_until:
                    report.errors.append(
                        f"Поиск #{search_num(owner, sid)}: пауза после ограничения Авито, "
                        f"ещё {max(1, int(blocked_until - time.time()))} с."
                    )
                    continue
                try:
                    cached = recent_reader_snapshot(url) if FETCH_MODE == "reader" else None
                    items = await fetch(session, url)
                except FetchError as exc:
                    reason = str(exc)
                    if isinstance(exc, Blocked):
                        block_streak = min(block_streak + 1, 5)
                        pause = min(3600, 300 * 2 ** (block_streak - 1))
                        blocked_until = time.time() + pause
                        reason += f" Повтор через {pause // 60} мин."
                    report.errors.append(f"Поиск #{search_num(owner, sid)}: {reason}")
                    log.warning("Ошибка поиска #%s: %s", sid, reason)
                    if FETCH_MODE == "reader":
                        snapshot = recent_reader_snapshot(url)
                        if snapshot:
                            report.notes.append(reader_snapshot_note(sid, owner, snapshot))
                    await report_error(owner, sid, reason)
                    continue
                except Exception:
                    reason = "Внутренняя ошибка проверки. Нужна проверка журналов Render."
                    log.error("Неожиданная ошибка поиска #%s", sid)
                    report.errors.append(f"Поиск #{search_num(owner, sid)}: {reason}")
                    await report_error(owner, sid, reason)
                    continue

                block_streak = 0
                blocked_until = 0.0
                error_notices.pop(sid, None)
                report.checked += 1
                report.found += len(items)
                current = db.execute(
                    "SELECT title,min_price,max_price,exclude,initialized,skip_reserved FROM searches WHERE id=?",
                    (sid,),
                ).fetchone()
                if current is None:
                    continue
                title, mn, mx, exclude, initialized, skip_reserved = current
                if cached:
                    report.notes.append(reader_snapshot_note(sid, owner, cached))
                    log.info("Поиск #%s: последний ответ, %s объявлений; новый запрос не нужен",
                             sid, len(items))
                else:
                    log.info("Поиск #%s: прочитано %s объявлений, источник %s", sid, len(items), FETCH_MODE)
                now = int(time.time())
                # Store seen state and the delivery queue in one transaction.
                with db:
                    for item in reversed(items):
                        cur = db.execute(
                            "INSERT OR IGNORE INTO items(search_id, item_id, price, seen_at) "
                            "VALUES(?,?,?,?)", (sid, item.id, item.price, now),
                        )
                        if cur.rowcount and initialized:
                            if passes_filters(item, mn, mx, exclude, skip_reserved):
                                db.execute(
                                    "INSERT OR IGNORE INTO pending_notifications"
                                    "(search_id, item_id, payload, created_at) VALUES(?,?,?,?)",
                                    (sid, item.id, json.dumps(asdict(item), ensure_ascii=False), now),
                                )
                            else:
                                report.filtered += 1
                    if not initialized:
                        db.execute("UPDATE searches SET initialized=1 WHERE id=?", (sid,))

                if not initialized:
                    try:
                        await bot.send_message(
                            owner,
                            f"Поиск #{search_num(owner, sid)} «{html.escape(title or 'поиск')}»: "
                            f"запомнил {len(items)} текущих объявлений. "
                            "Дальше присылаю только новые. Команда /check покажет результат проверки.",
                        )
                    except Exception:
                        log.warning("Не удалось сообщить об инициализации поиска #%s", sid)
                else:
                    # Avoid two attempts in one cycle if a previous pending send failed.
                    if delivery_ok:
                        await deliver_pending(session, sid, owner, report, context)
                await asyncio.sleep(random.uniform(4, 9))
        report.deferred = db.execute(
            "SELECT COUNT(*) FROM pending_notifications p JOIN searches s ON s.id=p.search_id" +
            (" WHERE s.chat_id=?" if chat_id is not None else ""),
            (chat_id,) if chat_id is not None else (),
        ).fetchone()[0]
    return report


def next_cycle_delay(started: float) -> float:
    now = time.monotonic()
    remaining = CHECK_INTERVAL - (now - started)
    if remaining <= 0:
        return CHECK_INTERVAL
    if FETCH_MODE == "reader":
        # A manual check can move a search's next refresh between regular cycles.
        # Wake at that deadline instead of delaying its next fetch for 10 minutes.
        for (url,) in db.execute("SELECT DISTINCT url FROM searches"):
            snapshot = recent_reader_snapshot(url)
            if snapshot:
                remaining = min(remaining, snapshot.started_at + READER_REFRESH_INTERVAL - now)
    return max(0.01, remaining)


async def scheduler():
    while True:
        started = time.monotonic()
        log.info("Автопроверка: начало цикла, интервал %s с", CHECK_INTERVAL)
        try:
            await run_check()
        except Exception:
            log.exception("Сбой цикла проверки")
        # Count from the previous start and avoid bursts after an overlong cycle.
        await asyncio.sleep(next_cycle_delay(started))


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
                (owner, url, title.strip() or default_title(url), DEFAULT_EXCLUDE),
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
    log.info("Источник объявлений: %s; интервал не меньше %s с", FETCH_MODE, CHECK_INTERVAL)
    seed_searches()
    asyncio.create_task(keep_awake())
    await wait_for_telegram()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

