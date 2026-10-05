"""
Webhook signing (HMAC-SHA256), shared by the sender script and the Catalog Service.

The sender signs   "<unix timestamp>.<raw body>"   with a shared secret and sends:
    X-Webhook-Timestamp: 1767225600
    X-Webhook-Signature: sha256=<hex digest>

The receiver recomputes the digest and also rejects old timestamps, so a
captured request cannot be replayed later.
"""

import hashlib
import hmac
import time

TIMESTAMP_HEADER = "X-Webhook-Timestamp"
SIGNATURE_HEADER = "X-Webhook-Signature"


class SignatureError(Exception):
    """Raised when a webhook signature is missing, malformed, old or wrong."""


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def signed_headers(secret: str, body: bytes) -> dict[str, str]:
    timestamp = int(time.time())
    return {TIMESTAMP_HEADER: str(timestamp), SIGNATURE_HEADER: sign(secret, timestamp, body)}


def verify(secret: str, timestamp_header: str | None, signature_header: str | None,
           body: bytes, tolerance_s: int = 300) -> None:
    """Raise SignatureError unless the request is correctly signed and recent."""
    if not timestamp_header or not signature_header:
        raise SignatureError("missing signature headers")
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        raise SignatureError("timestamp is not an integer") from None
    if abs(time.time() - timestamp) > tolerance_s:
        raise SignatureError("timestamp outside the allowed window")
    expected = sign(secret, timestamp, body)
    # compare_digest takes the same time whether or not the strings match,
    # so an attacker can't guess the signature byte by byte.
    if not hmac.compare_digest(expected, signature_header):
        raise SignatureError("signature does not match")