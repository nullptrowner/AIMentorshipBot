FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
COPY --from=ghcr.io/astral-sh/uv:0.9.16 /uv /uvx /usr/local/bin/
COPY --from=mwader/static-ffmpeg:9.0.2-1 /ffmpeg /ffprobe /usr/local/bin/
RUN useradd -u 10001 -m studio
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev && mkdir -p /app/data/media && chown -R studio:studio /app/data
COPY alembic.ini ./
COPY migrations ./migrations
USER studio
CMD ["team-ai", "serve"]
