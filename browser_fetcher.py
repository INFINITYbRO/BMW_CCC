"""Normal Chromium rendering with one reusable session; no CAPTCHA solving."""
import asyncio
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit


class BrowserFetchError(Exception):
    """Only fixed diagnostics may leave this module, never browser exceptions."""


class BrowserBlocked(BrowserFetchError):
    pass


def proxy_options(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        scheme = parts.scheme.lower()
        if scheme == "socks5h":
            scheme = "socks5"
        if scheme not in {"http", "https", "socks5"} or not parts.hostname or not parts.port:
            raise ValueError()
        if scheme == "socks5" and (parts.username or parts.password):
            raise BrowserFetchError("Chromium не поддерживает SOCKS5 с логином и паролем. Используй HTTP-прокси.")
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        options = {"server": f"{scheme}://{host}:{parts.port}"}
        if parts.username is not None:
            options["username"] = unquote(parts.username)
        if parts.password is not None:
            options["password"] = unquote(parts.password)
        return options
    except ValueError:
        raise BrowserFetchError("Неверный формат PROXY для Chromium.") from None


def document_identity(url: str) -> tuple:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (parts.scheme not in {"https", "http"} or parts.username or parts.password
                or parts.port not in (None, 80, 443)
                or not (host == "avito.ru" or host.endswith(".avito.ru"))):
            raise ValueError()
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key in ("context", "from", "from_item", "from_item_top"):
            query.pop(key, None)
        return (parts.path.rstrip("/"), tuple(sorted(query.items())))
    except ValueError:
        raise BrowserFetchError("Браузер получил неподходящую ссылку вместо страницы Авито.") from None


@dataclass
class BrowserDocument:
    html: str
    status: int


class BrowserFetcher:
    def __init__(self, proxy: str | None = None):
        self.proxy = proxy
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def _start(self):
        if self._browser is not None and self._browser.is_connected() and self._context is not None:
            return
        await self._close()
        options = proxy_options(self.proxy)
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            launch = {"headless": True, "channel": "chromium", "args": ["--disable-dev-shm-usage"]}
            if options:
                launch["proxy"] = options
            self._browser = await self._playwright.chromium.launch(**launch)
            self._context = await self._browser.new_context(
                locale="ru-RU", viewport={"width": 1280, "height": 800}, service_workers="block")
            await self._context.route("**/*", self._route)
        except Exception:
            await self._close()
            raise BrowserFetchError(
                "Chromium не запущен. Для режима browser установи браузер и его системные библиотеки; "
                "на Render используй Dockerfile из репозитория."
            ) from None

    async def _route(self, route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    async def read(self, url: str, detail: bool = False) -> BrowserDocument:
        expected = document_identity(url)
        async with self._lock:
            await self._start()
            page = None
            try:
                page = await self._context.new_page()
                status = None

                def navigated(response):
                    nonlocal status
                    if response.request.is_navigation_request() and response.frame == page.main_frame:
                        status = response.status

                page.on("response", navigated)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if status is None and response is not None:
                    status = response.status
                if status not in (404, 410):
                    selector = ('[data-marker="item-view/item-description"], [itemprop="description"]:not(meta)'
                                if detail else '[data-marker="item"], [data-marker="search-results/empty"]')
                    try:
                        await page.wait_for_function("""selector => Boolean(document.querySelector(selector)) ||
                            /ничего не найдено|ничего не нашлось|нет объявлений|доступ ограничен|подтвердите, что вы не робот/i
                            .test((document.title || '') + '\\n' + (document.body?.innerText || '').slice(0, 2000))""",
                                                     arg=selector, timeout=20000)
                    except Exception:
                        # Extract once after the wait; unknown/partial pages will
                        # be rejected by the listing/detail parser, never as empty.
                        pass
                if document_identity(page.url) != expected:
                    raise BrowserFetchError("Авито перенаправил браузер на другую страницу. Проверь ссылку поиска.")
                if status in (401, 403, 407, 429, 439):
                    raise BrowserBlocked(f"Авито ограничил браузерный доступ (HTTP {status}). Повторю позже.")
                if status is None or status >= 400 and status not in (404, 410):
                    raise BrowserFetchError("Браузер не получил успешный ответ страницы Авито.")
                return BrowserDocument(await page.content(), status)
            except BrowserFetchError:
                raise
            except Exception:
                raise BrowserFetchError(
                    "Браузер не смог загрузить Авито. Проверь доступ через PROXY; повторю позже."
                ) from None
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass

    async def _close(self):
        for resource, method in ((self._context, "close"), (self._browser, "close"), (self._playwright, "stop")):
            if resource is not None:
                try:
                    await getattr(resource, method)()
                except Exception:
                    pass
        self._context = self._browser = self._playwright = None

    async def close(self):
        async with self._lock:
            await self._close()
