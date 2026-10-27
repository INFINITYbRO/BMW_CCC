"""Credential-safe proxy diagnostics. Never return raw transport exceptions."""
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import RequestException


def connect_status(exc: RequestException) -> int | None:
    message = str(exc).lower()
    if "connect" not in message and "tunnel" not in message:
        return None
    match = re.search(r"(?:response|http(?:/\d(?:\.\d)?)?)\s*[:=]?\s*(\d{3})\b", message)
    return int(match[1]) if match else None


@dataclass
class ProxyProbe:
    http_status: int | None = None
    connect_status: int | None = None
    error: str = ""
    body: str = field(default="", repr=False)

    def line(self, label: str) -> str:
        if self.connect_status == 403:
            return f"{label}: прокси запретил HTTPS-туннель (CONNECT HTTP 403)."
        if self.connect_status == 407 or self.http_status == 407:
            return f"{label}: прокси требует авторизацию (HTTP 407)."
        if self.connect_status:
            return f"{label}: ошибка HTTPS-туннеля (CONNECT HTTP {self.connect_status})."
        if self.http_status is not None:
            return f"{label}: сайт ответил HTTP {self.http_status}; соединение через прокси установлено."
        reasons = {
            "config": "неверный формат PROXY (схема, адрес или порт)",
            "timeout": "тайм-аут подключения через прокси",
            "dns": "не удалось определить адрес прокси",
            "connect": "не удалось подключиться через прокси",
            "tls": "ошибка проверки TLS-сертификата",
            "socks": "прокси отклонил SOCKS-соединение",
            "transport": "сетевая ошибка подключения через прокси",
        }
        return f"{label}: {reasons.get(self.error, reasons['transport'])}."


async def probe_target(session, proxy: str, target: str) -> ProxyProbe:
    """Force this proxy, ignoring the bot's cooldown and all fallback routes."""
    try:
        parts = urlsplit(proxy.strip())
        if (parts.scheme.lower() not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
                or not parts.hostname or parts.port is None or parts.query or parts.fragment):
            return ProxyProbe(error="config")
    except ValueError:
        return ProxyProbe(error="config")
    try:
        response = await session.get(target, proxy=proxy.strip(), timeout=15,
                                     allow_redirects=True, max_redirects=5)
    except RequestException as exc:
        code = int(exc.code or 0)
        error = {5: "dns", 7: "connect", 28: "timeout", 60: "tls", 97: "socks"}.get(code, "transport")
        return ProxyProbe(connect_status=connect_status(exc), error=error)
    return ProxyProbe(http_status=response.status_code, body=response.text)


def proxy_conclusion(control: ProxyProbe, avito: ProxyProbe) -> str:
    if control.connect_status == 403 and avito.connect_status == 403:
        return ("Прокси запретил оба HTTPS-туннеля ещё до ответа сайтов. По HTTP 403 нельзя определить, "
                "истёк ли тариф или не разрешён IP сервера. Уточни у провайдера доступ с исходящих IP Render "
                "и поддержку HTTPS CONNECT; не меняй пароль наугад.")
    if any(p.connect_status == 407 or p.http_status == 407 for p in (control, avito)):
        return "Прокси запросил авторизацию. Проверь формат, логин и пароль PROXY в Render."
    if control.http_status is not None and avito.connect_status == 403:
        return "Контрольный HTTPS-сайт доступен, но прокси запрещает туннель к Авито. Уточни ограничение у провайдера."
    if avito.http_status in (403, 429, 439):
        return ("Соединение через прокси установлено, но Авито ограничивает эти запросы. "
                "Это ответ Авито, а не отказ прокси создать туннель.")
    if avito.http_status == 200:
        return "Авито ответил через настроенный прокси. Результат чтения объявлений указан выше."
    if control.http_status is not None:
        return "Прокси отвечает для контрольного HTTPS-сайта; доступ к Авито пока не подтверждён."
    return "Работа прокси с этого сервера не подтверждена. Проверь доступ у провайдера по результатам выше."
