FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps --no-shell chromium \
    && useradd --create-home appuser \
    && chown appuser:appuser /app

COPY --chown=appuser:appuser bot.py avito_parser.py proxy_diagnostics.py browser_fetcher.py ./
USER appuser
CMD ["python", "bot.py"]
