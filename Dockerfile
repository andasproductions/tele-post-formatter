FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

RUN uv run python -m spacy download en_core_web_md

# /data is where Railway mounts the persistent volume
RUN mkdir -p /data

CMD ["uv", "run", "python", "bot.py"]
