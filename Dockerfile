FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY . .
RUN pip install --no-cache-dir .
RUN python -m playwright install --with-deps chromium

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
