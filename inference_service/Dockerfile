FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .

RUN uv sync --no-dev --python /usr/local/bin/python3

COPY main.py .
COPY models.py .

EXPOSE 8001

CMD ["uv", "run", "--python", "/usr/local/bin/python3", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]