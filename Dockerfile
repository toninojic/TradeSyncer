# TradeCopier: one image with the API, the copy engine and the admin panel.
#
# The admin panel is shipped pre-built in frontend/dist, so no Node.js is needed
# here. After changing frontend code, run `npm run build` in frontend/ first.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/backend

# Install dependencies first (cached layer), then the code.
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install -e .

COPY frontend/dist /app/frontend/dist

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/backend/data \
    && chown -R appuser /app/backend/data
USER appuser

# Inside the container the server must listen on all interfaces; docker-compose
# publishes the port on 127.0.0.1 only, so it is still reachable from this PC only.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATABASE_URL=sqlite+aiosqlite:////app/backend/data/tradecopier.db

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

CMD ["python", "-m", "app"]
