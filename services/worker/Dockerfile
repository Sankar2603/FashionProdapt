# Build from the project root:
#   docker compose build worker
# The same image runs both the worker and the beat scheduler.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1. CPU-only PyTorch (same layer as the Retrieval Service, so Docker reuses it).
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# 2. Worker dependencies.
COPY services/worker/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# 3. Shared project code.
COPY libs/common /opt/libs/common
RUN pip install /opt/libs/common

# 4. Worker code.
COPY services/worker/app ./app

# Run as a non-root user; /hf-cache is where the model cache is mounted.
RUN useradd --create-home appuser && mkdir -p /hf-cache && chown appuser /hf-cache
USER appuser

CMD ["celery", "-A", "app.celery_app", "worker", "--pool=solo", \
     "--queues=catalog_sync,maintenance", "--loglevel=INFO"]