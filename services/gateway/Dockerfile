# Build from the project root:
#   docker compose build gateway
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1. Service dependencies (no ML libraries: the gateway only orchestrates).
COPY services/gateway/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# 2. Shared project code.
COPY libs/common /opt/libs/common
RUN pip install /opt/libs/common

# 3. Service code.
COPY services/gateway/app ./app

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]