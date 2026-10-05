# Build from the project root:
#   docker compose build catalog
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1. Service dependencies (no ML libraries: this service only writes Postgres
#    and puts jobs on the queue).
COPY services/catalog_service/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# 2. Shared project code.
COPY libs/common /opt/libs/common
RUN pip install /opt/libs/common

# 3. Service code.
COPY services/catalog_service/app ./app

RUN useradd --create-home appuser
USER appuser

EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]