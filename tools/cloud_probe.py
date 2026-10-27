"""Read-only deployment check: no Telegram token, proxy or browser cookies."""
import argparse
import asyncio
import json
import os
import re
from pathlib import Path

URL = (
    "https://www.avito.ru/sochi/igry_pristavki_i_programmy/"
    "igry_pristavki_i_programmy/igrovye_pristavki_i_aksessuary/"
    "igrovye_pristavki-ASgBAgICAkSSAsoJ9M0UmsqPAw?q=ps+vita&s=104"
)


def summarize(method, status, page):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "lxml")
    cards = soup.select('[data-marker="item"]')
    items = sum(bool(card.select_one('a[data-marker="item-title"], a[itemprop="url"]'))
                for card in cards)
    lowered = page.lower()
    result = {
        "method": method,
        "http_status": status,
        "parsed_cards": items,
        "access_limited": any(text in lowered for text in (
            "доступ ограничен", "подтвердите, что вы не робот", "captcha")),
        "page_bytes": len(page.encode()),
        "success": items > 0 and status == 200,
    }
    return result


async def reader_check(check_details=False):
    os.environ["BOT_TOKEN"] = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    os.environ["DB_PATH"] = ":memory:"
    os.environ["AVITO_FETCH_MODE"] = "reader"
    os.environ["PROXY"] = ""  # public connectivity test, no configured credentials
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import bot
    from curl_cffi.requests import AsyncSession
    async with AsyncSession(impersonate="chrome", trust_env=False) as session:
        try:
            items = await bot.fetch_reader(session, bot.normalize_url(URL))
            result = {"method": "reader", "parsed_cards": len(items),
                      "priced_cards": sum(bool(item.price) for item in items),
                      "success": bool(items), "fresh_page_required": True}
            if check_details and items:
                try:
                    detailed = await bot.fetch_details(session, items[0])
                    result.update(description_chars=len(detailed.description),
                                  reserved=detailed.reserved, active=detailed.active,
                                  detail_success=True)
                except bot.FetchError as exc:
                    result.update(success=False, detail_success=False, reason=str(exc))
            return result
        except bot.FetchError as exc:
            return {"method": "reader", "success": False, "reason": str(exc)}


def curl_check():
    from curl_cffi import requests
    try:
        response = requests.get(URL, impersonate="chrome", proxies={"all": ""}, timeout=35)
        return summarize("curl", response.status_code, response.text)
    except Exception as exc:
        return {"method": "curl", "success": False, "error_type": type(exc).__name__}


async def browser_check():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(locale="ru-RU", viewport={"width":1280,"height":800})
            page = await context.new_page()
            response = await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            # Allow normal page JavaScript; do not attempt to solve any CAPTCHA.
            await page.wait_for_timeout(5000)
            return summarize("browser", response.status if response else None, await page.content())
        except Exception as exc:
            return {"method": "browser", "success": False, "error_type": type(exc).__name__}
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", choices=("curl", "browser", "reader"), required=True)
    parser.add_argument("--details", action="store_true", help="Also verify one complete listing through Reader")
    args = parser.parse_args()
    if args.method == "reader":
        result = asyncio.run(reader_check(args.details))
    else:
        result = curl_check() if args.method == "curl" else asyncio.run(browser_check())
    output = json.dumps(result, ensure_ascii=False)
    print(output, flush=True)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("### Avito free-cloud connectivity check\n\n")
            summary.write("```json\n" + output + "\n```\n")
    raise SystemExit(0 if result["success"] else 1)

