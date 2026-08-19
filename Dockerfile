FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv==0.11.6 \
    && adduser --system --group --no-create-home paloma

COPY pyproject.toml README.md uv.lock ./
RUN uv sync --frozen --no-dev --extra fsq --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --extra fsq

USER paloma

ENTRYPOINT ["paloma-data"]
CMD ["--help"]
