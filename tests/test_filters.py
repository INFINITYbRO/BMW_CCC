"""Full-description/reservation regressions, without external network calls."""
import json
import os
import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["BOT_TOKEN"] = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["DB_PATH"] = ":memory:"
import bot
from avito_parser import (clean_exclusions, is_reserved, parse_details_html,
                          parse_details_markdown)
from test_bot import MonitorHarness, item

REAL_FETCH_DETAILS = bot.fetch_details
REAL_FETCH_DIRECT_PAGE = bot.fetch_direct_page

DETAIL = '''<html><head>
<link rel="canonical" href="https://www.avito.ru/sochi/sony_1234567890">
</head><body><h1>Sony PS Vita</h1>
<div data-marker="item-view/item-status">{status}</div>
<div data-marker="item-view/item-description">{description}</div>
<section><article data-marker="item"><span data-marker="item-description">
На запчасти, забронировано</span></article></section></body></html>'''


def listing():
    return replace(item("1234567890"), url="https://www.avito.ru/sochi/sony_1234567890?context=tracking")


def reader_response(page, status=200, url=None):
    return SimpleNamespace(status_code=200, json=lambda: {"data": {
        "url": url or bot.listing_url(listing().url), "httpStatus": status, "html": page,
    }})


class FullDescriptionParserTests(unittest.TestCase):
    def test_seo_excerpt_is_not_used_instead_of_full_description(self):
        page = '<meta itemprop="description" content="Короткое описание">' + DETAIL.format(
            status="", description="Полный текст. На запчасти")
        details = parse_details_html(page, "1234567890")
        self.assertEqual(details.description, "Полный текст. На запчасти")

    def test_only_own_description_and_status_are_used(self):
        details = parse_details_html(DETAIL.format(status="", description="Работает отлично"), "1234567890")
        self.assertEqual(details.description, "Работает отлично")
        self.assertFalse(details.reserved)
        self.assertTrue(details.active)

    def test_full_description_is_not_truncated(self):
        description = "Отличное состояние. " * 300 + "Не включается"
        details = parse_details_html(DETAIL.format(status="", description=description), "1234567890")
        self.assertIn("Не включается", details.description)
        self.assertFalse(bot.passes_filters(replace(listing(), description=details.description), 0, 0,
                                            "не включается"))

    def test_status_badge_marks_reserved(self):
        details = parse_details_html(DETAIL.format(status="Забронировано", description="Всё работает"), "1234567890")
        self.assertTrue(details.reserved)

    def test_schema_product_description_and_availability(self):
        product = {"@type": "Product", "url": bot.listing_url(listing().url),
                   "description": "Чехол &amp; приставка", "offers": {"availability": "https://schema.org/Reserved"}}
        details = parse_details_html('<script type="application/ld+json">' + json.dumps(product) + '</script>', "1234567890")
        self.assertEqual(details.description, "Чехол & приставка")
        self.assertTrue(details.reserved)

    def test_sold_product_is_inactive(self):
        page = DETAIL.format(status="Объявление снято с публикации", description="")
        self.assertFalse(parse_details_html(page, "1234567890").active)

    def test_foreign_product_and_recommendations_cannot_supply_description(self):
        product = {"@type": "Product", "url": "https://www.avito.ru/sochi/other_1234567891", "description": "text"}
        page = '<script type="application/ld+json">' + json.dumps(product) + '</script>'
        page += '<article data-marker="item"><span itemprop="description">other</span></article>'
        with self.assertRaises(ValueError):
            parse_details_html(page, "1234567890")

    def test_wrong_canonical_card_rejected(self):
        with self.assertRaises(ValueError):
            parse_details_html(DETAIL.format(status="", description="text"), "1234567891")

    def test_missing_description_is_not_a_success(self):
        with self.assertRaises(ValueError):
            parse_details_html("<h1>Подтвердите, что вы не робот</h1>", "1234567890")

    def test_markdown_description_stops_before_seller_and_recommendations(self):
        details = parse_details_markdown("# Sony PS Vita\n## Описание\nВсё работает\n"
                                         "## Местоположение\nСочи\n## Похожие объявления\nЗабронировано")
        self.assertEqual(details.description, "Всё работает")
        self.assertFalse(details.reserved)

    def test_markdown_setext_description_and_reservation(self):
        details = parse_details_markdown("Забронировано\n\nОписание\n--------\nВ хорошем состоянии\n")
        self.assertTrue(details.reserved)
        self.assertEqual(details.description, "В хорошем состоянии")

    def test_reservation_statements(self):
        for text in ("Забронировано", "Товар забронирован", "В РЕЗЕРВЕ", "Зарезервирована",
                     "Работает отлично\nБРОНЬ", "Sony Vita (бронь)", "Бронь до вечера"):
            with self.subTest(text=text):
                self.assertTrue(is_reserved(text))

    def test_offers_and_negated_reservations_are_not_reserved(self):
        for text in ("Без брони", "Не забронировано", "Не в резерве", "Брони нет", "Бронь снята",
                     "Резерв отменён", "Возможна бронь", "Можно забронировать", "Оформить бронь",
                     "Бронь не ставлю", "Не зарезервирована"):
            with self.subTest(text=text):
                self.assertFalse(is_reserved(text))

    def test_comma_whitespace_case_and_yo_normalization(self):
        self.assertEqual(clean_exclusions("  ЧЕХОЛ,чехол,  НЕТ  , , на\tзапчасти"), "чехол, нет, на запчасти")
        self.assertFalse(bot.passes_filters(replace(listing(), description="Всё сломано"), 0, 0,
                                            "чехол,все   сломано"))

    def test_reserved_can_be_allowed_but_closed_listing_cannot(self):
        self.assertFalse(bot.passes_filters(replace(listing(), reserved=True), 0, 0, ""))
        self.assertTrue(bot.passes_filters(replace(listing(), reserved=True), 0, 0, "", False))
        self.assertFalse(bot.passes_filters(replace(listing(), active=False), 0, 0, "", False))

    def test_old_database_migrates_without_losing_searches_history_or_pending(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript('''
          CREATE TABLE searches(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            url TEXT NOT NULL, title TEXT, min_price INTEGER DEFAULT 0, max_price INTEGER DEFAULT 0,
            exclude TEXT DEFAULT '', initialized INTEGER DEFAULT 0);
          INSERT INTO searches(chat_id,url,title,exclude,initialized) VALUES(11,'url','Vita','чехол',1);
          CREATE TABLE items(search_id INTEGER,item_id TEXT,price INTEGER,seen_at INTEGER,
            PRIMARY KEY(search_id,item_id));
          INSERT INTO items VALUES(1,'123',9000,1);
          CREATE TABLE pending_notifications(search_id INTEGER,item_id TEXT,payload TEXT NOT NULL,
            created_at INTEGER NOT NULL,PRIMARY KEY(search_id,item_id));
          INSERT INTO pending_notifications VALUES(1,'123','{}',1);
        ''')
        bot.init_db(connection)
        bot.init_db(connection)
        self.assertEqual(connection.execute("SELECT exclude,initialized,skip_reserved FROM searches").fetchone(),
                         ("чехол", 1, 1))
        self.assertEqual(connection.execute("SELECT price FROM items").fetchone(), (9000,))
        self.assertEqual(connection.execute("SELECT payload FROM pending_notifications").fetchone(), ("{}",))


class DetailNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = [patch.object(bot, "detail_snapshots", {}), patch.object(bot, "PROXY", None),
                        patch.object(bot, "fetch_direct_page", new=AsyncMock(side_effect=bot.FetchError("Direct offline"))),
                        patch.object(bot.asyncio, "sleep", new=AsyncMock())]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()

    async def test_reader_sends_only_canonical_listing_url_and_no_credentials(self):
        session = SimpleNamespace(get=AsyncMock(return_value=reader_response(DETAIL.format(status="", description="Полное описание"))))
        with patch.object(bot, "FETCH_MODE", "reader"), patch.object(bot, "PROXY", "secret-proxy"):
            result = await bot.fetch_details(session, listing())
        args, kwargs = session.get.await_args
        self.assertEqual(args[0], "https://r.jina.ai/https://www.avito.ru/sochi/sony_1234567890")
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("proxy", kwargs)
        self.assertEqual(kwargs["proxies"], {"all": ""})
        self.assertEqual(result.description, "Полное описание")

    async def test_redirect_to_another_listing_is_rejected(self):
        session = SimpleNamespace(get=AsyncMock(return_value=reader_response(
            DETAIL.format(status="", description="text"), url="https://www.avito.ru/sochi/other_1234567891")))
        with patch.object(bot, "FETCH_MODE", "reader"), self.assertRaises(bot.FetchError):
            await bot.fetch_details(session, listing())

    async def test_target_439_is_never_accepted_as_a_detail_page(self):
        session = SimpleNamespace(get=AsyncMock(return_value=reader_response(
            DETAIL.format(status="", description="text"), status=439)))
        with patch.object(bot, "FETCH_MODE", "reader"), self.assertRaisesRegex(bot.FetchError, "439"):
            await bot.fetch_details(session, listing())

    async def test_direct_missing_listing_is_inactive(self):
        session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(text="Gone", status_code=410)))
        with patch.object(bot, "FETCH_MODE", "direct"), patch.object(bot, "fetch_direct_page", REAL_FETCH_DIRECT_PAGE):
            result = await bot.fetch_details(session, listing())
        self.assertFalse(result.active)

    async def test_failed_check_is_cached_then_retried_after_expiry(self):
        with patch.object(bot, "FETCH_MODE", "reader"), patch.object(bot, "read_item_details",
                new=AsyncMock(side_effect=[bot.FetchError("HTTP 429"), listing()])) as read, patch.object(
                bot, "fetch_direct_page", new=AsyncMock(side_effect=bot.FetchError("Direct offline"))):
            for _ in range(2):
                with self.assertRaises(bot.FetchError):
                    await bot.fetch_details(None, listing())
            self.assertEqual(read.await_count, 1)
            next(iter(bot.detail_snapshots.values())).started_at -= 301
            await bot.fetch_details(None, listing())
            self.assertEqual(read.await_count, 2)

    async def test_cached_details_do_not_replace_new_search_price(self):
        with patch.object(bot, "FETCH_MODE", "reader"), patch.object(bot, "read_item_details",
                new=AsyncMock(return_value=replace(listing(), description="Полный текст"))) as read:
            await bot.fetch_details(None, listing())
            current = await bot.fetch_details(None, replace(listing(), price=7000))
            self.assertEqual((read.await_count, current.price, current.description), (1, 7000, "Полный текст"))

    async def test_existing_proxy_is_used_if_reader_is_unavailable(self):
        page = DETAIL.format(status="", description="Полный текст через прокси")
        with patch.object(bot, "FETCH_MODE", "reader"), patch.object(bot, "PROXY", "existing-proxy"), patch.object(
                bot, "read_item_details", new=AsyncMock(side_effect=bot.FetchError("Reader offline"))), patch.object(
                bot, "fetch_direct_page", new=AsyncMock(return_value=(page, 200))) as direct:
            result = await bot.fetch_details(None, listing())
        self.assertEqual(result.description, "Полный текст через прокси")
        direct.assert_awaited_once()

    async def test_direct_fallback_works_without_any_new_proxy_setting(self):
        page = DETAIL.format(status="", description="Полное описание напрямую")
        with patch.object(bot, "FETCH_MODE", "reader"), patch.object(bot, "PROXY", None), patch.object(
                bot, "read_item_details", new=AsyncMock(side_effect=bot.FetchError("HTTP 439"))), patch.object(
                bot, "fetch_direct_page", new=AsyncMock(return_value=(page, 200))) as direct:
            result = await bot.fetch_details(None, listing())
        self.assertEqual(result.description, "Полное описание напрямую")
        direct.assert_awaited_once()


class FilterDeliveryTests(MonitorHarness):
    # Reuse the same isolated database/network harness as existing monitor tests.
    async def test_reader_search_to_full_html_to_filter_without_mocking_parsers(self):
        self.search(exclude="на запчасти")
        search_url = bot.normalize_url("https://www.avito.ru/sochi?q=ps+vita")
        search_response = SimpleNamespace(status_code=200, json=lambda: {"data": {
            "url": search_url, "httpStatus": 200,
            "content": "[Sony PS Vita](https://www.avito.ru/sochi/sony_1234567890)\n\n9 000 ₽\nХорошее состояние",
        }})
        detail_response = reader_response(DETAIL.format(status="", description="На запчасти, не включается"))
        http = SimpleNamespace(get=AsyncMock(side_effect=[search_response, detail_response]))
        self.session.__aenter__.return_value = http
        with patch.object(bot, "FETCH_MODE", "reader"), patch.object(bot, "fetch_details", REAL_FETCH_DETAILS), patch.object(
                bot, "notify", new=AsyncMock()) as notify:
            report = await bot.run_check(11)
        self.assertEqual((report.checked, report.filtered, report.sent, self.pending()), (1, 1, 0, 0))
        self.assertEqual(http.get.await_count, 2)
        notify.assert_not_awaited()

    async def test_word_only_in_full_description_filters_new_listing(self):
        self.search(exclude="на запчасти")
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[listing()])), patch.object(
                bot, "fetch_details", new=AsyncMock(return_value=replace(listing(), description="На запчасти"))), patch.object(
                bot, "notify", new=AsyncMock()) as notify:
            report = await bot.run_check(11)
        self.assertEqual((report.filtered, report.sent, self.pending()), (1, 0, 0))
        notify.assert_not_awaited()

    async def test_reservation_only_on_full_page_is_filtered(self):
        self.search()
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[listing()])), patch.object(
                bot, "fetch_details", new=AsyncMock(return_value=replace(listing(), reserved=True))):
            report = await bot.run_check(11)
        self.assertEqual((report.filtered, report.sent, self.pending()), (1, 0, 0))

    async def test_detail_failure_for_one_item_does_not_block_another_chat(self):
        self.search(chat=11)
        self.search(chat=22)
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[listing()])), patch.object(
                bot, "fetch_details", new=AsyncMock(side_effect=[bot.FetchError("Bad card"), listing()])):
            report = await bot.run_check()
        self.assertEqual((report.sent, report.deferred), (1, 1))

    async def test_failed_description_check_is_retried_even_after_item_leaves_search(self):
        self.search()
        with patch.object(bot, "fetch", new=AsyncMock(side_effect=[[listing()], []])), patch.object(
                bot, "fetch_details", new=AsyncMock(side_effect=[bot.FetchError("HTTP 429"), listing()])):
            first = await bot.run_check(11)
            self.assertEqual((first.sent, first.deferred, self.pending()), (0, 1, 1))
            second = await bot.run_check(11)
        self.assertEqual((second.sent, second.deferred, self.pending()), (1, 0, 0))

    async def test_retry_rechecks_reservation_after_a_telegram_failure(self):
        self.search()
        self.telegram.send_message.side_effect = RuntimeError("Telegram offline")
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[listing()])), patch.object(
                bot, "fetch_details", new=AsyncMock(side_effect=[listing(), replace(listing(), reserved=True)])):
            await bot.run_check(11)
            self.assertEqual(self.pending(), 1)
            self.telegram.send_message.side_effect = None
            report = await bot.run_check(11)
        self.assertEqual((report.filtered, report.sent, self.pending()), (1, 0, 0))

    async def test_disabled_reservation_filter_sends_without_extra_detail_requests(self):
        sid = self.search()
        self.db.execute("UPDATE searches SET skip_reserved=0 WHERE id=?", (sid,))
        self.db.commit()
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[replace(listing(), reserved=True)])), patch.object(
                bot, "fetch_details", new=AsyncMock()) as detail:
            report = await bot.run_check(11)
        self.assertEqual(report.sent, 1)
        detail.assert_not_awaited()

    async def test_delete_while_fetching_does_not_leave_orphan_queue_or_send(self):
        sid = self.search()
        async def fetched(*args):
            self.db.execute("DELETE FROM searches WHERE id=?", (sid,))
            self.db.commit()
            return [listing()]
        with patch.object(bot, "fetch", new=AsyncMock(side_effect=fetched)):
            report = await bot.run_check(11)
        self.assertEqual((report.sent, self.pending()), (0, 0))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    async def test_filter_edit_during_detail_fetch_takes_effect_before_delivery(self):
        sid = self.search()
        async def detailed(*args):
            self.db.execute("UPDATE searches SET exclude='запчасти' WHERE id=?", (sid,))
            self.db.commit()
            return replace(listing(), description="На запчасти")
        with patch.object(bot, "fetch", new=AsyncMock(return_value=[listing()])), patch.object(
                bot, "fetch_details", new=AsyncMock(side_effect=detailed)):
            report = await bot.run_check(11)
        self.assertEqual((report.sent, report.filtered), (0, 1))

    async def test_burst_details_are_bounded_and_rest_remain_queued(self):
        self.search()
        candidates = [replace(listing(), id=str(1234567890 + n),
                              url=f"https://www.avito.ru/sochi/sony_{1234567890 + n}") for n in range(12)]
        with patch.object(bot, "fetch", new=AsyncMock(return_value=candidates)), patch.object(
                bot, "fetch_details", new=AsyncMock(side_effect=lambda session, x: x)) as detail:
            report = await bot.run_check(11)
            self.assertEqual((detail.await_count, report.sent, report.deferred), (10, 10, 2))
            report = await bot.run_check(11)
        self.assertEqual((report.sent, report.deferred), (2, 0))

    async def test_reserved_command_is_scoped_to_owned_search(self):
        mine = self.search(chat=11)
        other = self.search(chat=22)
        message = SimpleNamespace(chat=SimpleNamespace(id=11), from_user=SimpleNamespace(id=11), answer=AsyncMock())
        with patch.object(bot, "ALLOWED_IDS", set()):
            await bot.cmd_reserved(message, SimpleNamespace(args="1 off"))
        self.assertEqual(self.db.execute("SELECT skip_reserved FROM searches WHERE id=?", (mine,)).fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT skip_reserved FROM searches WHERE id=?", (other,)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
