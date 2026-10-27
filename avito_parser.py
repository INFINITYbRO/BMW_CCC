"""Parsers for an individual public listing; no network or credentials."""
import html
import json
import re
import unicodedata
from dataclasses import dataclass

from bs4 import BeautifulSoup


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", html.unescape(text)).casefold()
                    .replace("ё", "е").split())


def clean_exclusions(words: str) -> str:
    return ", ".join(dict.fromkeys(normalize_text(w) for w in words.split(",")
                                  if normalize_text(w) and w.strip() != "-"))


def is_reserved(text: str) -> bool:
    """Positive reservation statements, without matching offers to reserve."""
    # Keep line boundaries: sellers often put a single «БРОНЬ» on its own line.
    text = "\n".join(normalize_text(line) for line in text.splitlines())
    text = re.sub(
        r"\b(?:не\s+(?:в\s+резерве|забронирован\w*|зарезервирован\w*)|"
        r"(?:без|нет)\s+(?:брони|резерва)|(?:брони|резерва)\s+нет|"
        r"(?:бронь|резерв)\s+(?:снят\w*|отменен\w*|не\s+ставлю))\b", "", text,
    )
    return bool(re.search(
        r"\b(?:забронирован(?:о|а|ы)?|зарезервирован(?:о|а|ы)?|в\s+резерве)\b", text
    ) or re.search(r"(?:^|[.!;\n(])\s*(?:бронь|резерв)(?:\s+до\b[^.!;]*)?\s*(?:$|[.!;\n)])",
                   text) or re.search(r"\b(?:бронь|резерв)\s+до\b", text))


@dataclass(frozen=True)
class ListingDetails:
    description: str
    reserved: bool = False
    active: bool = True


def _products(value):
    """Only schema roots / @graph, never recommendations nested in a carousel."""
    if isinstance(value, list):
        for node in value:
            yield from _products(node)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if "Product" in types:
            yield value
        yield from _products(value.get("@graph", []))


def _availability(product: dict) -> str:
    offers = product.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return str(offers.get("availability", "")) if isinstance(offers, dict) else ""


def parse_details_html(page: str, item_id: str) -> ListingDetails:
    soup = BeautifulSoup(page, "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical:
        identity = re.search(r"_(\d+)(?:$|[?#])", canonical.get("href", ""))
        if identity and identity[1] != item_id:
            raise ValueError("Получена карточка другого объявления")
    # A valid detail page must have its own description or product schema.
    # Never use the whole page: it includes similar listings and seller reviews.
    candidates = soup.select('[data-marker="item-view/item-description"], [data-marker="item-description"]')
    candidates += soup.select('[itemprop="description"]:not(meta)')
    node = next((n for n in candidates
                 if not n.find_parent(attrs={"data-marker": "item"})), None)
    description = None
    if node is not None:
        description = (node.get("content", "") if node.name == "meta"
                       else node.get_text("\n", strip=True))
    statuses = [n.get_text(" ", strip=True) for n in soup.select(
        '[data-marker^="item-view/"][data-marker*="status"], '
        '[data-marker="item-status"], [data-marker*="reservation"], '
        '[data-marker="item-view/closed-warning"]') if not n.find_parent(attrs={"data-marker": "item"})]
    availability = ""
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            products = list(_products(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
        for product in products:
            identity = str(product.get("url") or product.get("sku") or product.get("productID") or "")
            if identity and not re.search(r"(?:^|[_/])" + re.escape(item_id) + r"(?:$|[?#/])", identity):
                continue
            if description is None and isinstance(product.get("description"), str):
                description = BeautifulSoup(product["description"], "lxml").get_text("\n", strip=True)
            availability = _availability(product)
            break
        if availability or description is not None:
            break
    availability_node = next((n for n in soup.select('[itemprop="availability"]')
                              if not n.find_parent(attrs={"data-marker": "item"})), None)
    if availability_node:
        availability = availability_node.get("href") or availability_node.get("content", availability)
    status = normalize_text(" ".join(statuses))
    active = not (availability.rstrip("/").rsplit("/", 1)[-1].lower() in
                  {"soldout", "outofstock", "discontinued"}
                  or any(word in status for word in
                         ("объявление снято", "объявление закрыто", "продано", "уже продан")))
    reserved = availability.rstrip("/").rsplit("/", 1)[-1].lower() == "reserved" or is_reserved("\n".join(statuses))
    if description is None and active:
        raise ValueError("Не найдено полное описание объявления")
    description = html.unescape(description or "")
    title = soup.select_one('h1')
    own_text = (title.get_text(" ", strip=True) if title else "") + "\n" + description
    return ListingDetails(description, reserved or is_reserved(own_text), active)


def parse_details_markdown(page: str) -> ListingDetails:
    # Cut off recommendation cards before looking for reservation labels.
    own = re.split(r"(?im)^\s*(?:#{1,6}\s*)?(?:Похожие объявления|Другие объявления продавца)\s*$",
                   page, maxsplit=1)[0]
    heading = re.search(r"(?im)^\s*(?:#{1,6}\s+)?Описание\s*:?\s*\n(?:[-=]+\s*\n)?", own)
    if not heading:
        raise ValueError("Не найдено полное описание объявления")
    description = re.split(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:Местоположение|Продавец|Отзывы|Похожие объявления|"
        r"Другие объявления продавца|Пожаловаться|Номер объявления)\b[^\n]*$",
        own[heading.end():], maxsplit=1,
    )[0].strip()
    # A badge is normally a separate line before the description. Do not treat
    # seller reviews, a booking button, or a similar listing as the badge.
    status_lines = own[:heading.start()].splitlines()
    reserved = any(is_reserved(line.strip(" #*")) for line in status_lines)
    active = not any(normalize_text(line.strip(" #*")) in
                     {"объявление снято с публикации", "объявление закрыто", "продано"}
                     for line in status_lines)
    return ListingDetails(description, reserved or is_reserved(description), active)
