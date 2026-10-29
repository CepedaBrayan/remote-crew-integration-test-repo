import hashlib
import hmac

from fastapi import HTTPException
from utils import audit_failure


async def auth(raw_body: bytes, signature: str, secret: str):
    """Verify BigPMS HMAC-SHA256 against the exact request body bytes.

    Args:
        raw_body: Unmodified HTTP body; parsing or reserializing changes the signature.
        signature: Hex digest from X-BigPMS-Signature, or an empty string if absent.
        secret: Shared secret used when registering the callback.

    Raises:
        HTTPException: HTTP 401 after best-effort auditing of an invalid signature.
    """
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8")):
        audit_failure(raw_body, "Invalid signature")
        raise HTTPException(status_code=401, detail="Invalid signature")
