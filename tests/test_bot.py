"""Offline regression tests. No real Telegram/Avito requests or credentials."""
import asyncio
import html
import json
import os
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["BOT_TOKEN"] = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["DB_PATH"] = ":memory:"
os.environ.pop("PROXY", None)
import bot

CARD = """<article data-marker="item" data-item-id="123">
<a data-marker="item-title" href="/sochi/console_123" title="PS Vita"></a>
<meta itemprop="price" content="9000"><span data-marker="item-address">Сочи</span>
</article>"""


def item(number="new", title="PS Vita"):
    return bot.Item(number, title, 9000, "https://www.avito.ru/sochi/console_" + number,
                    "Сочи", "сегодня", None, "")


def response(body=CARD, status=200):
    return SimpleNamespace(text=body, status_code=status)


class ParserTests(unittest.TestCase):
    def test_article_card(self):
        parsed = bot.parse_items(CARD)
        self.assertEqual(len(parsed), 1)
        self.assertEqual((parsed[0].id, parsed[0].price), ("123", 9000))

    def test_reject_similar_domains_and_credentials(self):
        for url in ("https://avito.ru.evil.test/", "https://evilavito.ru/",
                    "https://user:pass@avito.ru/", "ftp://avito.ru/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                bot.normalize_url(url)

    def test_sort_first_page(self):
        url = bot.normalize_url("https://www.avito.ru/sochi?q=ps+vita&p=5&s=1")
        self.assertIn("s=104", url)
        self.assertNotIn("p=", url)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = patch.multiple(bot, PROXY="http://secret:password@proxy.invalid:8080",
                                     PROXY_FALLBACK=True, proxy_retry_at=0.0, connection_note="")
        self.config.start()

    async def asyncTearDown(self):
        self.config.stop()

    async def test_proxy_403_falls_back_without_environment_proxy(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=[
            bot.RequestException("CONNECT tunnel failed, response 403; secret:password", code=7),
            response(),
        ]))
        self.assertEqual(len(await bot.fetch(session, "https://www.avito.ru/sochi")), 1)
        self.assertEqual(session.get.await_args_list[1].kwargs["proxies"], {"all": ""})
        self.assertNotIn("secret", bot.connection_note)
        self.assertIn("HTTP 403", bot.connection_note)
        self.assertGreater(bot.proxy_retry_at, 0)
        session.get.reset_mock()
        session.get.side_effect = None
        session.get.return_value = response()
        await bot.fetch(session, "https://www.avito.ru/sochi")
        self.assertEqual(session.get.await_args.kwargs["proxies"], {"all": ""})

    async def test_can_disable_fallback(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=bot.RequestException(
            "CONNECT tunnel failed, response 403", code=7)))
        with patch.object(bot, "PROXY_FALLBACK", False), self.assertRaises(bot.FetchError):
            await bot.fetch(session, "https://www.avito.ru/sochi")
        self.assertEqual(session.get.await_count, 1)

    async def test_direct_avito_block_not_reported_as_success(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=[
            bot.RequestException("CONNECT tunnel failed, response 403", code=7),
            response("Доступ ограничен", 403),
        ]))
        with self.assertRaisesRegex(bot.Blocked, "напрямую"):
            await bot.fetch(session, "https://www.avito.ru/sochi")

    async def test_unrecognized_page_with_broken_card_is_an_error(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(
            '<div data-marker="item">broken</div>')))
        with self.assertRaises(bot.FetchError):
            await bot.fetch(session, "https://www.avito.ru/sochi")

    async def test_empty_search_is_valid(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response("Ничего не нашлось")))
        self.assertEqual(await bot.fetch(session, "https://www.avito.ru/sochi"), [])


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        schema = "\n".join(bot.db.iterdump())
        self.db.executescript(schema)
        self.telegram = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
        self.session = MagicMock()
        self.session.__aenter__ = AsyncMock(return_value=MagicMock())
        self.session.__aexit__ = AsyncMock(return_value=False)
        self.patches = [
            patch.object(bot, "db", self.db),
            patch.object(bot, "bot", self.telegram),
            patch.object(bot, "check_lock", asyncio.Lock()),
            patch.object(bot, "blocked_until", 0.0),
            patch.object(bot, "block_streak", 0),
            patch.object(bot, "connection_note", ""),
            patch.object(bot, "error_notices", {}),
            patch.object(bot, "AsyncSession", return_value=self.session),
            patch.object(bot.asyncio, "sleep", new=AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()

    def search(self, chat=11, initialized=1, exclude=""):
        sid = self.db.execute(
            "INSERT INTO searches(chat_id,url,title,initialized,exclude) VALUES(?,?,?,?,?)",
            (chat, "https://www.avito.ru/sochi?q=ps+vita", "Vita", initialized, exclude)
        ).lastrowid
        self.db.commit()
        return sid

    def pending(self):
        return self.db.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[0]

    async def test_failed_delivery_survives_disappearing_listing_and_fetch_failure(self):
        self.search()
        self.telegram.send_message.side_effect = RuntimeError("Telegram temporarily unavailable")
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[item()])):
            report = await bot.run_check(chat_id=11)
        self.assertEqual(report.sent, 0)
        self.assertEqual(self.pending(), 1)
        self.telegram.send_message.side_effect = None
        with patch.object(bot, "fetch", new=AsyncMock(side_effect=bot.FetchError("Сеть недоступна"))):
            report = await bot.run_check(chat_id=11)
        self.assertEqual(report.sent, 1)
        self.assertEqual(self.pending(), 0)

    async def test_baseline_is_silent_and_new_item_is_sent_once(self):
        self.search(initialized=0)
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[item("old")])):
            first = await bot.run_check(11)
        self.assertEqual(first.sent, 0)
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[item("new"), item("old")])):
            second = await bot.run_check(11)
            third = await bot.run_check(11)
        self.assertEqual((second.sent, third.sent, self.pending()), (1, 0, 0))

    async def test_filtered_item_not_queued(self):
        self.search(exclude="чехол")
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[item(title="Чехол PS Vita")])):
            report = await bot.run_check(11)
        self.assertEqual((report.filtered, report.sent, self.pending()), (1, 0, 0))

    async def test_manual_check_is_scoped_to_chat(self):
        self.search(chat=11)
        self.search(chat=22)
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[])) as fetch:
            report = await bot.run_check(11)
        self.assertEqual((fetch.await_count, report.checked), (1, 1))

    async def test_fetch_errors_are_visible_and_rate_limited(self):
        self.search()
        with patch.object(bot, "fetch", new=AsyncMock(side_effect=bot.FetchError("Прокси отклонил соединение"))):
            report = await bot.run_check(11)
            await bot.run_check(11)
        self.assertIn("Прокси отклонил", report.text())
        self.assertEqual(self.telegram.send_message.await_count, 1)
        self.assertEqual(report.checked, 0)

    async def test_busy_and_backoff_have_clear_reports(self):
        self.search()
        async with bot.check_lock:
            report = await bot.run_check(11)
        self.assertIn("уже идёт", report.text())
        with patch.object(bot, "blocked_until", bot.time.time() + 60):
            report = await bot.run_check(11)
        self.assertIn("пауза", report.text())

    async def test_pending_retried_during_avito_backoff(self):
        sid = self.search()
        self.db.execute("INSERT INTO pending_notifications VALUES(?,?,?,?)",
                        (sid, "new", json.dumps(bot.asdict(item())), 1))
        self.db.commit()
        with patch.object(bot, "blocked_until", bot.time.time() + 60):
            report = await bot.run_check(11)
        self.assertEqual((report.sent, self.pending()), (1, 0))

    async def test_photo_failure_uses_intact_html_text(self):
        sid = self.search()
        listing = item(title="<Vita & friends>")
        listing.image = "https://example.test/image.jpg"
        self.telegram.send_photo.side_effect = RuntimeError("photo download failed")
        await bot.notify(11, sid, "Vita", listing)
        caption = self.telegram.send_message.await_args.args[1]
        self.assertIn("&lt;Vita &amp; friends&gt;", caption)
        self.assertIn("<tg-emoji", caption)
        self.assertTrue(caption.endswith("Vita"))

    async def test_failed_send_in_one_chat_does_not_block_another(self):
        self.search(chat=11)
        self.search(chat=22)
        async def send(chat_id, *args, **kwargs):
            if chat_id == 11:
                raise RuntimeError("unavailable")
        self.telegram.send_message.side_effect = send
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[item()])):
            report = await bot.run_check()
        self.assertEqual(report.sent, 1)
        self.assertEqual(self.pending(), 1)


if __name__ == "__main__":
    unittest.main()

