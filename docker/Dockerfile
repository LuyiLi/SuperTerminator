FROM python:3.11-slim

ENV UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --frozen

COPY app ./app
COPY data/.gitkeep ./data/.gitkeep

EXPOSE 8080

CMD ["uv", "run", "python", "-m", "app.main"]
