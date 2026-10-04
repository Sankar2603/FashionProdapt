# Build from the project root:
#   docker compose build rerank                                    (CPU)
#   docker compose -f docker-compose.yml -f docker-compose.gpu.yml build rerank   (NVIDIA GPU)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1. PyTorch. CPU build by default; the GPU compose file passes a CUDA index URL.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install torch --index-url ${TORCH_INDEX_URL}

# 2. Service dependencies.
COPY services/rerank_service/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# 3. Shared project code.
COPY libs/common /opt/libs/common
RUN pip install /opt/libs/common

# 4. Service code.
COPY services/rerank_service/app ./app

# Non-root user; /hf-cache must be writable if the model is downloaded there.
RUN useradd --create-home appuser \
    && mkdir -p /hf-cache \
    && chown appuser:appuser /hf-cache
USER appuser

EXPOSE 8004
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004", "--no-access-log"]