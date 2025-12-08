FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7392

# System deps for curl_cffi / cryptography wheels fallback
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        libcurl4-openssl-dev \
        curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY main.py pyproject.toml README.md ./
COPY static ./static

RUN pip install --no-cache-dir \
    "fastapi[standard]>=0.124.0" \
    "jmcomic>=2.6.10" \
    "standard-imghdr>=3.13.0" \
    "uvicorn>=0.38.0" \
    "watchdog>=6.0.0"

EXPOSE ${PORT}

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7392"]
