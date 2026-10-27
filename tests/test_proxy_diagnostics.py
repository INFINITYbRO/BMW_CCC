"""Distinguish proxy CONNECT failures from target-site responses without leaks."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from proxy_diagnostics import ProxyProbe, connect_status, probe_target, proxy_conclusion
from test_bot import MonitorHarness, CARD, response

PROXY = "http://test-user:test-password@proxy.invalid:8080"


class ProxyTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_forces_configured_proxy_and_never_tries_direct_fallback(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response("ok")))
        result = await probe_target(session, PROXY, "https://github.com/robots.txt")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(session.get.await_count, 1)
        self.assertEqual(session.get.await_args.kwargs["proxy"], PROXY)
        self.assertNotIn("proxies", session.get.await_args.kwargs)

    async def test_connect_403_is_distinguished_and_credentials_are_not_returned(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=bot.RequestException(
            "CONNECT tunnel failed, response 403; " + PROXY, code=7)))
        result = await probe_target(session, PROXY, "https://www.avito.ru/")
        self.assertEqual((result.http_status, result.connect_status), (None, 403))
        self.assertIn("CONNECT HTTP 403", result.line("Авито"))
        for value in (repr(result), result.line("Авито"), result.body):
            self.assertNotIn("test-password", value)
            self.assertNotIn("test-user", value)
        self.assertEqual(session.get.await_count, 1)

    async def test_actual_site_403_does_not_become_a_connect_failure(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response("Forbidden", 403)))
        result = await probe_target(session, PROXY, "https://www.avito.ru/")
        self.assertEqual((result.http_status, result.connect_status), (403, None))
        self.assertNotIn("CONNECT HTTP 403", result.line("Авито"))

    async def test_proxy_auth_407_is_explicit(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=bot.RequestException(
            "CONNECT tunnel failed, response 407", code=56)))
        result = await probe_target(session, PROXY, "https://www.avito.ru/")
        self.assertEqual(result.connect_status, 407)
        self.assertIn("авторизацию", result.line("Авито"))

    async def test_timeout_does_not_echo_transport_exception(self):
        session = SimpleNamespace(get=AsyncMock(side_effect=bot.RequestException(
            "Operation timed out: " + PROXY, code=28)))
        result = await probe_target(session, PROXY, "https://www.avito.ru/")
        self.assertEqual(result.error, "timeout")
        self.assertNotIn("test-password", result.line("Авито"))

    async def test_malformed_proxy_never_reaches_network(self):
        session = SimpleNamespace(get=AsyncMock())
        for value in ("bad-value", "http://user:pass@host:bad", "ftp://host:80", "http://host"):
            result = await probe_target(session, value, "https://www.avito.ru/")
            self.assertEqual(result.error, "config")
        session.get.assert_not_awaited()

    async def test_body_is_not_in_result_repr_or_diagnostic_line(self):
        result = ProxyProbe(http_status=200, body="potentially-sensitive-body")
        self.assertNotIn("potentially-sensitive-body", repr(result))
        self.assertNotIn("potentially-sensitive-body", result.line("Авито"))

    async def test_old_proxy_error_is_cleared_when_tunnel_recovers(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response(CARD)))
        with patch.multiple(bot, PROXY=PROXY, proxy_retry_at=0.0, connection_note="old CONNECT failure"):
            await bot.fetch_direct_page(session, "https://www.avito.ru/sochi")
            self.assertEqual(bot.connection_note, "")

    async def test_site_limit_after_recovery_does_not_repeat_old_tunnel_failure(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response("Too many requests", 429)))
        with patch.multiple(bot, PROXY=PROXY, proxy_retry_at=0.0, connection_note="old CONNECT failure"):
            with self.assertRaises(bot.Blocked):
                await bot.fetch_direct_page(session, "https://www.avito.ru/sochi")
            self.assertEqual(bot.connection_note, "")


class ProxyDiagnosticReasonTests(unittest.TestCase):
    def test_general_403_does_not_claim_payment_or_password_failure(self):
        result = proxy_conclusion(ProxyProbe(connect_status=403), ProxyProbe(connect_status=403))
        self.assertIn("По HTTP 403 нельзя определить", result)
        self.assertNotIn("неверный пароль", result)
        self.assertNotIn("просрочен", result)

    def test_control_succeeds_but_avito_tunnel_is_denied(self):
        result = proxy_conclusion(ProxyProbe(http_status=200), ProxyProbe(connect_status=403))
        self.assertIn("Контрольный HTTPS-сайт доступен", result)
        self.assertIn("туннель к Авито", result)

    def test_site_rate_limit_is_not_a_proxy_auth_failure(self):
        result = proxy_conclusion(ProxyProbe(http_status=200), ProxyProbe(http_status=429))
        self.assertIn("Авито ограничивает", result)
        self.assertNotIn("авторизацию", result)

    def test_main_network_message_403_is_specific_and_safe(self):
        reason = bot.network_error(bot.RequestException("CONNECT tunnel failed, response 403; " + PROXY, code=7), True)
        self.assertIn("/proxy", reason)
        self.assertIn("CONNECT HTTP 403", reason)
        self.assertNotIn("test-password", reason)


class ProxyCommandTests(MonitorHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.extra_patches = [patch.object(bot, "PROXY", PROXY), patch.object(bot, "ALLOWED_IDS", {11}),
                              patch.object(bot, "proxy_probe_reports", {}),
                              patch.object(bot, "proxy_probe_lock", asyncio.Lock())]
        for p in self.extra_patches:
            p.start()
        self.message = SimpleNamespace(chat=SimpleNamespace(id=11), from_user=SimpleNamespace(id=11), answer=AsyncMock())

    async def asyncTearDown(self):
        for p in reversed(self.extra_patches):
            p.stop()
        await super().asyncTearDown()

    async def test_unauthorized_user_cannot_trigger_proxy_requests(self):
        self.message.from_user.id = 22
        with patch.object(bot, "build_proxy_report", new=AsyncMock()) as build:
            await bot.cmd_proxy(self.message)
        self.message.answer.assert_not_awaited()
        build.assert_not_awaited()

    async def test_missing_proxy_does_not_probe_network(self):
        with patch.object(bot, "PROXY", None), patch.object(bot, "build_proxy_report", new=AsyncMock()) as build:
            await bot.cmd_proxy(self.message)
        build.assert_not_awaited()
        self.assertIn("не задан", self.message.answer.await_args.args[0])

    async def test_repeated_command_uses_cached_report_and_preserves_proxy_cooldown(self):
        with patch.object(bot, "build_proxy_report", new=AsyncMock(return_value="safe report")) as build, patch.object(
                bot, "proxy_retry_at", 123456.0):
            await bot.cmd_proxy(self.message)
            await bot.cmd_proxy(self.message)
            self.assertEqual(bot.proxy_retry_at, 123456.0)
        self.assertEqual(build.await_count, 1)
        self.assertIn("Последняя проверка", self.message.answer.await_args.args[0])

    async def test_unexpected_failure_is_safe_and_cached(self):
        with patch.object(bot, "build_proxy_report", new=AsyncMock(side_effect=RuntimeError(PROXY))):
            await bot.cmd_proxy(self.message)
        self.assertNotIn("test-password", self.message.answer.await_args.args[0])
        self.assertNotIn("test-password", repr(bot.proxy_probe_reports))

    async def test_full_diagnostic_uses_three_forced_proxy_requests(self):
        self.search()
        card = CARD.replace('data-item-id="123"', 'data-item-id="1234567890"').replace('console_123"', 'console_1234567890"')
        page = '<h1>PS Vita</h1><div data-marker="item-view/item-description">В хорошем состоянии</div>'
        http = SimpleNamespace(get=AsyncMock(side_effect=[response("robots"), response(card), response(page)]))
        self.session.__aenter__.return_value = http
        report = await bot.build_proxy_report(11)
        self.assertEqual(http.get.await_count, 3)
        for call in http.get.await_args_list:
            self.assertEqual(call.kwargs["proxy"], PROXY)
            self.assertNotIn("r.jina.ai", call.args[0])
        self.assertIn("Карточек в выдаче: 1", report)
        self.assertIn("Полное описание распознано", report)
        self.assertNotIn("test-password", report)

    async def test_general_proxy_denial_does_not_become_zero_listings(self):
        self.search()
        http = SimpleNamespace(get=AsyncMock(side_effect=bot.RequestException(
            "CONNECT tunnel failed, response 403; " + PROXY, code=7)))
        self.session.__aenter__.return_value = http
        report = await bot.build_proxy_report(11)
        self.assertEqual(http.get.await_count, 2)
        self.assertIn("оба HTTPS-туннеля", report)
        self.assertNotIn("Карточек в выдаче: 0", report)
        self.assertNotIn("test-password", report)

    async def test_success_http_without_cards_does_not_verify_full_description(self):
        self.search()
        http = SimpleNamespace(get=AsyncMock(return_value=response("Challenge")))
        self.session.__aenter__.return_value = http
        report = await bot.build_proxy_report(11)
        self.assertIn("не подтверждает чтение", report)
        self.assertNotIn("Полное описание распознано", report)
        self.assertEqual(http.get.await_count, 2)


if __name__ == "__main__":
    unittest.main()
