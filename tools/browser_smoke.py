"""Real Chromium test: JS-rendered cards, full description and session cookies.

All HTTPS pages are fulfilled locally; no Avito/Telegram requests or real keys.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["BOT_TOKEN"] = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["DB_PATH"] = ":memory:"
os.environ["AVITO_FETCH_MODE"] = "browser"
os.environ["PROXY"] = ""
import bot

SEARCH = "https://www.avito.ru/browser-fixture?q=ps+vita&s=104"
DETAIL = "https://www.avito.ru/browser-fixture/sony_1234567890"
CARD = '<article data-marker="item" data-item-id="1234567890"><a data-marker="item-title" href="' + DETAIL + '">PS Vita</a><meta itemprop="price" content="9000"></article>'


async def main():
    try:
        await bot.browser_reader._start()

        async def fixture(route):
            if route.request.url == SEARCH:
                body = ("<html><body><script>document.cookie='browser_fixture=yes; path=/';"
                        "setTimeout(() => document.body.innerHTML = " + json.dumps(CARD) + ", 50);</script></body></html>")
            else:
                body = ("<html><body><script>setTimeout(() => { const d = document.createElement('div');"
                        "d.dataset.marker='item-view/item-description';"
                        "d.textContent = document.cookie.includes('browser_fixture=yes') ? 'На запчасти, не включается' : 'NO COOKIE';"
                        "document.body.appendChild(d); }, 50);</script></body></html>")
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        await bot.browser_reader._context.route("https://www.avito.ru/browser-fixture**", fixture)
        items = await bot.fetch_browser(SEARCH)
        assert len(items) == 1 and items[0].price == 9000
        detailed = await bot.read_item_details(None, items[0])
        assert detailed.description == "На запчасти, не включается", repr(detailed.description)
        assert not bot.passes_filters(detailed, 0, 0, "не включается")
        print(json.dumps({"success": True, "javascript_cards": len(items),
                          "session_cookie_preserved": True, "full_description_filter": True}))
    finally:
        await bot.browser_reader.close()


if __name__ == "__main__":
    asyncio.run(main())
