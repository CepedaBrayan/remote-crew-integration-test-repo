import hashlib
import hmac

from fastapi import HTTPException
from utils import audit_failure


async def auth(raw_body: bytes, signature: str, secret: str):
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8")):
        audit_failure(raw_body, "Invalid signature")
        raise HTTPException(status_code=401, detail="Invalid signature")
