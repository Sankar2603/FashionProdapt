import os
import httpx
import psycopg
import redis
from dotenv import load_dotenv

load_dotenv()


def check_redis():
    client = redis.from_url(os.getenv("REDIS_URL"))
    client.ping()
    return "PONG received"


def check_qdrant():
    url = os.getenv("QDRANT_URL")
    response = httpx.get(f"{url}/", timeout=3)
    response.raise_for_status()
    return "reachable"


def check_postgres():
    conn = psycopg.connect(
        host=os.getenv("POSTGRES_HOST"),
        port=os.getenv("POSTGRES_PORT"),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )
    conn.close()
    return "connected"


def main():
    checks = [
        ("Redis", check_redis),
        ("Qdrant", check_qdrant),
        ("Postgres", check_postgres),
    ]

    for name, check in checks:
        try:
            print(f"[OK]   {name}: {check()}")
        except Exception as e:
            print(f"[FAIL] {name}: {e}")


if __name__ == "__main__":
    main()