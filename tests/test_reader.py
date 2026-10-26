import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import os
os.environ["BOT_TOKEN"] = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["DB_PATH"] = ":memory:"
import bot

URL = bot.normalize_url("https://www.avito.ru/sochi?q=ps+vita")
MARKDOWN = """[![Image 1: Sony PS Vita, Сочи](https://img.example/photo.jpg)](https://www.avito.ru/sochi/games/sony_vita_1234567890?context=tracking)

**9 500₽**

Sony

Чехол в комплекте

1 час назад

[](https://www.avito.ru/sochi/games/ps_vita_1234567891)

**8 000₽**

14 дней на возврат

Sony Vita в хорошем состоянии

Вчера

[PS Vita](https://www.avito.ru/sochi/games/vita_1234567892)

**7 500₽**

Описание третьего объявления
"""


def response(status=200, target_status=200, content=MARKDOWN, url=URL):
    return SimpleNamespace(status_code=status, json=lambda: {
        "data": {"url": url, "content": content, "httpStatus": target_status}
    })


class ReaderParserTests(unittest.TestCase):
    def test_three_cards_have_correct_boundaries_and_prices(self):
        items = bot.parse_reader_items(MARKDOWN)
        self.assertEqual(len(items), 3)
        self.assertEqual([i.price for i in items], [9500, 8000, 7500])
        self.assertEqual(items[0].title, "Sony PS Vita")
        self.assertEqual(items[0].place, "Сочи")
        self.assertEqual(items[1].title, "ps vita")
        self.assertEqual(items[2].title, "PS Vita")
        self.assertNotIn("третьего", items[0].description)
        self.assertNotIn("context=", items[0].url)

    def test_delivery_promise_is_not_listing_date(self):
        items = bot.parse_reader_items(MARKDOWN)
        self.assertEqual(items[0].date, "1 час назад")
        self.assertEqual(items[1].date, "Вчера")
        self.assertNotIn("возврат", items[1].date)

    def test_deduplicates_listing_links(self):
        self.assertEqual(len(bot.parse_reader_items(MARKDOWN + MARKDOWN)), 3)

    def test_only_avito_listing_links(self):
        text = MARKDOWN.replace("www.avito.ru/", "www.avito.ru.evil.test/")
        self.assertEqual(bot.parse_reader_items(text), [])

    def test_existing_exclusions_apply_to_reader_descriptions(self):
        item = bot.parse_reader_items(MARKDOWN)[0]
        self.assertFalse(bot.passes_filters(item, 0, 0, "чехол"))


class ReaderNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sleep_patch = patch.object(bot.asyncio, "sleep", new=AsyncMock())
        self.sleep_patch.start()
        self.snapshot_patch = patch.object(bot, "reader_snapshots", {})
        self.snapshot_patch.start()

    async def asyncTearDown(self):
        self.snapshot_patch.stop()
        self.sleep_patch.stop()

    async def test_redirect_metadata_with_real_cards_is_usable(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(target_status=302)))
        items = await bot.fetch_reader(session, URL)
        self.assertEqual(len(items), 3)
        self.assertEqual(session.get.await_count, 1)
        self.assertTrue(session.get.await_args.kwargs["allow_redirects"])

    async def test_redirect_document_retries_with_browser_then_reads_final_cards(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=[
            response(target_status=302, content="Redirecting…"), response(),
        ]))
        self.assertEqual(len(await bot.fetch_reader(session, URL)), 3)
        self.assertEqual(session.get.await_count, 2)
        headers = session.get.await_args_list[1].kwargs["headers"]
        self.assertEqual(headers["X-Engine"], "browser")
        self.assertEqual(headers["X-Respond-Timing"], "resource-idle")

    async def test_unresolved_redirect_is_not_an_empty_success(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(
            target_status=302, content="Ничего не найдено")))
        with self.assertRaisesRegex(bot.FetchError, "перенаправил"):
            await bot.fetch_reader(session, URL)
        self.assertEqual(session.get.await_count, 2)

    async def test_outer_redirect_is_retried(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=[response(status=302), response()]))
        self.assertEqual(len(await bot.fetch_reader(session, URL)), 3)

    async def test_partial_content_is_retried_once(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=[response(content="Загрузка…"), response()]))
        self.assertEqual(len(await bot.fetch_reader(session, URL)), 3)
        self.assertEqual(session.get.await_count, 2)

    async def test_redirect_to_other_search_is_rejected(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(
            target_status=302, url="https://www.avito.ru/sochi?q=other&s=104")))
        with self.assertRaises(bot.FetchError):
            await bot.fetch_reader(session, URL)
        self.assertEqual(session.get.await_count, 1)

    async def test_redirect_without_a_priced_card_does_not_initialize_search(self):
        content = "[Переход](https://www.avito.ru/sochi/games/vita_1234567892)"
        session = SimpleNamespace(get=AsyncMock(return_value=response(target_status=302, content=content)))
        with self.assertRaisesRegex(bot.FetchError, "перенаправил"):
            await bot.fetch_reader(session, URL)

    async def test_fresh_public_url_without_any_proxy_or_credentials(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response()))
        with patch.object(bot, "FETCH_MODE", "reader"), patch.object(bot, "PROXY", "secret-proxy"):
            items = await bot.fetch(session, URL)
        self.assertEqual(len(items), 3)
        args, kwargs = session.get.await_args
        self.assertEqual(args, ("https://r.jina.ai/" + URL,))
        self.assertEqual(kwargs["proxies"], {"all": ""})
        self.assertEqual(kwargs["headers"]["X-Cache-Tolerance"], "0")
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("proxy", kwargs)

    async def test_target_error_is_rejected_even_when_reader_http_is_200(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(target_status=403)))
        with self.assertRaisesRegex(bot.FetchError, "HTTP 403"):
            await bot.fetch_reader(session, URL)

    async def test_target_439_is_not_retried_or_accepted_as_cards(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(target_status=439)))
        with self.assertRaisesRegex(bot.FetchError, "временно ограничил.*HTTP 439"):
            await bot.fetch_reader(session, URL)
        self.assertEqual(session.get.await_count, 1)

    async def test_wrong_search_response_is_rejected(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(
            url="https://www.avito.ru/sochi?q=other&s=104")))
        with self.assertRaises(bot.FetchError):
            await bot.fetch_reader(session, URL)

    async def test_empty_unknown_page_does_not_initialize_search(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(content="Страница проверки")))
        with self.assertRaises(bot.FetchError):
            await bot.fetch_reader(session, URL)

    async def test_valid_empty_search(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(content="Ничего не найдено")))
        self.assertEqual(await bot.fetch_reader(session, URL), [])

    async def test_service_rate_limit_is_visible(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(status=429)))
        with self.assertRaisesRegex(bot.FetchError, "частоту"):
            await bot.fetch_reader(session, URL)

    async def test_bad_json_is_safe(self):
        session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            status_code=200, json=lambda: {"unexpected": "response"})))
        with self.assertRaises(bot.FetchError):
            await bot.fetch_reader(session, URL)


if __name__ == "__main__":
    unittest.main()

