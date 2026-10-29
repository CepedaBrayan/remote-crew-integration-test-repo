import logging
from uuid import uuid4

import psycopg2
from orm import record_failure

log = logging.getLogger("leaseflow.webhooks")


def audit_failure(raw_body: bytes, reason: str):
    """Attempt to record a failed delivery without changing its HTTP outcome.

    A fresh receipt UUID prevents rejected deliveries from overwriting trusted
    event records. Database failures are logged and suppressed because auditing
    cannot succeed while storage is unavailable.
    """
    # If the database is unavailable, preserve the HTTP outcome and log the failure.
    try:
        uuid = uuid4()
        record_failure(uuid, raw_body, reason)
    except psycopg2.Error:
        log.exception("Could not record failed webhook: %s", reason)
