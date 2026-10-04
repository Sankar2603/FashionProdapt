# Build from the project root:
#   docker compose build retrieval
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1. CPU-only PyTorch. The default Linux wheel bundles CUDA (several GB),
#    which this service does not need: it encodes one short query at a time.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# 2. Service dependencies (cached unless requirements.txt changes).
COPY services/retrieval_service/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# 3. Shared project code.
COPY libs/common /opt/libs/common
RUN pip install /opt/libs/common

# 4. Service code.
COPY services/retrieval_service/app ./app

# Run as a non-root user. /hf-cache is the model cache; it must be owned by
# appuser so a fresh Docker volume mounted there is writable for the download.
RUN useradd --create-home appuser \
    && mkdir -p /hf-cache \
    && chown appuser:appuser /hf-cache
USER appuser

EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003", "--no-access-log"]