"""Browser routing, status verification, session reuse and queue regressions."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from browser_fetcher import (BrowserFetcher, BrowserDocument, BrowserFetchError,
                             BrowserBlocked, document_identity, proxy_options)
from test_bot import MonitorHarness, CARD, item

URL = "https://www.avito.ru/sochi?q=ps+vita&s=104"
REAL_FETCH_DETAILS = bot.fetch_details


class BrowserConfigurationTests(unittest.TestCase):
    def test_proxy_separates_and_decodes_credentials(self):
        result = proxy_options("http://user%40name:pass%3Aword@proxy.invalid:5500")
        self.assertEqual(result, {"server": "http://proxy.invalid:5500", "username": "user@name", "password": "pass:word"})
        self.assertNotIn("pass", result["server"])

    def test_direct_browser_and_unauthenticated_socks(self):
        self.assertIsNone(proxy_options(None))
        self.assertEqual(proxy_options("socks5h://proxy.invalid:5500"), {"server": "socks5://proxy.invalid:5500"})

    def test_invalid_proxy_errors_are_safe(self):
        for value in ("bad-value-with-secret", "http://user:secret@host:bad", "socks5://user:secret@host:123"):
            with self.subTest(value=value), self.assertRaises(BrowserFetchError) as error:
                proxy_options(value)
            self.assertNotIn("secret", str(error.exception))

    def test_only_avito_documents_without_embedded_credentials(self):
        for url in ("https://avito.ru.evil.test/", "https://user:secret@avito.ru/", "file:///etc/passwd"):
            with self.assertRaises(BrowserFetchError):
                document_identity(url)

    def test_tracking_and_canonical_host_do_not_change_identity(self):
        self.assertEqual(document_identity(URL), document_identity(URL.replace("www.avito.ru", "avito.ru") + "&context=tracking"))
        self.assertNotEqual(document_identity(URL), document_identity(URL.replace("ps+vita", "switch")))


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reader = BrowserFetcher()
        self.frame = object()
        self.page = SimpleNamespace(
            main_frame=self.frame, url=URL, on=MagicMock(),
            goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            wait_for_function=AsyncMock(), content=AsyncMock(return_value=CARD), close=AsyncMock())
        self.context = SimpleNamespace(new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.browser = SimpleNamespace(is_connected=lambda: True, close=AsyncMock())
        self.reader._context = self.context
        self.reader._browser = self.browser

    async def asyncTearDown(self):
        await self.reader.close()

    async def test_session_is_reused_and_pages_are_closed(self):
        await self.reader.read(URL)
        await self.reader.read(URL)
        self.assertIs(self.reader._context, self.context)
        self.assertEqual(self.context.new_page.await_count, 2)
        self.assertEqual(self.page.close.await_count, 2)

    async def test_http_439_with_cards_is_not_accepted(self):
        self.page.goto.return_value.status = 439
        with self.assertRaisesRegex(BrowserBlocked, "439"):
            await self.reader.read(URL)
        self.page.close.assert_awaited_once()

    async def test_final_main_document_status_after_normal_navigation_is_used(self):
        def event(status):
            return SimpleNamespace(status=status, frame=self.frame,
                                   request=SimpleNamespace(is_navigation_request=lambda: True))
        def listener(event_name, callback):
            self.callback = callback
        self.page.on.side_effect = listener
        self.page.goto.return_value.status = 302
        async def ready(*args, **kwargs):
            self.callback(event(200))
        self.page.wait_for_function.side_effect = ready
        document = await self.reader.read(URL)
        self.assertEqual(document.status, 200)

    async def test_redirect_to_another_search_is_rejected(self):
        self.page.url = URL.replace("ps+vita", "switch")
        with self.assertRaisesRegex(BrowserFetchError, "другую страницу"):
            await self.reader.read(URL)

    async def test_browser_exception_never_exposes_credentials(self):
        self.page.goto.side_effect = RuntimeError("proxy user:private-password@host")
        with self.assertRaises(BrowserFetchError) as error:
            await self.reader.read(URL)
        self.assertNotIn("private-password", str(error.exception))
        self.page.close.assert_awaited_once()

    async def test_detail_404_does_not_wait_for_a_description(self):
        self.page.goto.return_value.status = 404
        result = await self.reader.read(URL, detail=True)
        self.assertEqual(result.status, 404)
        self.page.wait_for_function.assert_not_awaited()

    async def test_images_are_blocked_but_scripts_are_rendered(self):
        route = SimpleNamespace(request=SimpleNamespace(resource_type="image"), abort=AsyncMock(), continue_=AsyncMock())
        await self.reader._route(route)
        route.abort.assert_awaited_once()
        route.request.resource_type = "script"
        await self.reader._route(route)
        route.continue_.assert_awaited_once()

    async def test_invalid_url_never_opens_a_browser_page(self):
        with self.assertRaises(BrowserFetchError):
            await self.reader.read("https://evil.invalid/")
        self.context.new_page.assert_not_awaited()


class BrowserMonitorTests(MonitorHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reader = SimpleNamespace(read=AsyncMock())
        self.extra = [patch.object(bot, "FETCH_MODE", "browser"), patch.object(bot, "browser_reader", self.reader)]
        for p in self.extra:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.extra):
            p.stop()
        await super().asyncTearDown()

    async def test_dynamic_full_description_filters_without_reader_or_curl(self):
        self.search(exclude="не включается")
        self.reader.read.side_effect = [BrowserDocument(CARD, 200), BrowserDocument(
            '<div data-marker="item-view/item-description">Не включается</div>', 200)]
        with patch.object(bot, "fetch_details", REAL_FETCH_DETAILS), patch.object(bot, "notify", new=AsyncMock()) as notify:
            report = await bot.run_check(11)
        self.assertEqual((report.checked, report.sent, report.filtered, report.deferred), (1, 0, 1, 0))
        self.assertEqual(self.reader.read.await_count, 2)
        self.assertTrue(self.reader.read.await_args.kwargs["detail"])
        bot.fetch_direct.assert_not_awaited()
        notify.assert_not_awaited()

    async def test_unknown_browser_page_does_not_initialize_empty_search(self):
        sid = self.search(initialized=0)
        self.reader.read.return_value = BrowserDocument("<h1>Проверка браузера</h1>", 200)
        report = await bot.run_check(11)
        self.assertEqual(report.checked, 0)
        self.assertEqual(self.db.execute("SELECT initialized FROM searches WHERE id=?", (sid,)).fetchone()[0], 0)

    async def test_recognized_empty_search_is_valid(self):
        self.search(initialized=0)
        self.reader.read.return_value = BrowserDocument('<div data-marker="search-results/empty">Ничего не найдено</div>', 200)
        report = await bot.run_check(11)
        self.assertEqual((report.checked, report.found), (1, 0))

    async def test_blocked_browser_is_throttled_and_never_falls_back(self):
        self.search()
        self.reader.read.side_effect = BrowserBlocked("HTTP 403")
        await bot.run_check(11)
        await bot.run_check(11)
        self.assertEqual(self.reader.read.await_count, 1)
        bot.fetch_direct.assert_not_awaited()

    async def test_repeated_manual_check_uses_last_browser_result(self):
        self.search(initialized=0)
        self.reader.read.return_value = BrowserDocument(CARD, 200)
        await bot.run_check(11)
        report = await bot.run_check(11)
        self.assertEqual(self.reader.read.await_count, 1)
        self.assertIn("Последняя успешная проверка", report.text())

    async def test_missing_detail_is_not_notified(self):
        self.search()
        self.reader.read.side_effect = [BrowserDocument(CARD, 200), BrowserDocument("Gone", 410)]
        with patch.object(bot, "fetch_details", REAL_FETCH_DETAILS):
            report = await bot.run_check(11)
        self.assertEqual((report.sent, report.filtered, report.deferred), (0, 1, 0))


if __name__ == "__main__":
    unittest.main()
