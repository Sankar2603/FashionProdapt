# Build from the project root:
#   docker compose build frontend
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1. Frontend dependencies.
COPY frontend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# 2. Shared project code. Only fashion_common.webhooks (standard library) is
#    used, so skip its database / Qdrant dependencies.
COPY libs/common /opt/libs/common
RUN pip install --no-deps /opt/libs/common

# 3. Frontend code.
COPY frontend/ ./

RUN useradd --create-home appuser
USER appuser

EXPOSE 8501
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"
CMD ["streamlit", "run", "app.py", "--server.address", "0.0.0.0", "--server.port", "8501", "--server.headless", "true"]
