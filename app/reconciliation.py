"""Compare SQL payment aggregates with one validated bulk BigPMS response."""

import logging
import time
from datetime import datetime, timezone

import psycopg2
import requests
from fastapi import HTTPException
from orm import read_reconciliation_snapshot
from pydantic import BaseModel, Field
from sync import retry_delay

log = logging.getLogger("leaseflow.reconciliation")


class ResidentSummary(BaseModel):
    """Require nonnegative integer cents and a nonempty partner resident identifier."""

    resident_id: str = Field(min_length=1)
    total_payments_cents: int = Field(strict=True, ge=0)


class PartnerSummaries(BaseModel):
    """Require the bulk resident list; extra partner metadata is permitted and unused."""

    residents: list[ResidentSummary]


def fetch_payment_totals(api_url: str) -> dict[str, int]:
    """Fetch all partner totals in one bulk request, with at most three attempts.

    Retry connection/timeouts, 429 (honoring Retry-After) and 5xx. Back off one and
    two seconds otherwise. Fail permanent HTTP errors and malformed/duplicate
    summaries immediately. Never call per-resident endpoints or infer a missing
    total. Exceptions are mapped by the report service to HTTP 503.
    """
    for attempt in range(3):
        delay = float(2**attempt)
        try:
            response = requests.get(
                f"{api_url.rstrip('/')}/api/v1/residents/summaries", timeout=(3, 10)
            )
            if response.status_code == 429:
                delay = retry_delay(response.headers.get("Retry-After"), delay)
            response.raise_for_status()
        except requests.HTTPError as error:
            if error.response.status_code != 429 and error.response.status_code < 500:
                raise
            failure = error
        except (requests.ConnectionError, requests.Timeout) as error:
            failure = error
        else:
            summaries = PartnerSummaries.model_validate(response.json())
            totals = {row.resident_id: row.total_payments_cents for row in summaries.residents}
            if len(totals) != len(summaries.residents):
                raise ValueError("Duplicate resident in partner summaries")
            return totals
        if attempt == 2:
            raise failure
        log.warning("Summary retry attempt=%s/3 delay=%ss error=%s", attempt + 1, delay, failure)
        time.sleep(delay)
    raise RuntimeError("Unreachable summary retry state")


def build_reconciliation_report(api_url: str) -> dict:
    """Return a read-only point-in-time comparison, or HTTP 503 when incomplete.

    Fetch the partner before opening a local read transaction. Missing local
    resident summaries are a partner-contract failure; unknown partner residents
    are rejected because this assessment assumes the seeded resident roster is
    shared. Delta is local minus partner cents. Count mismatched residents apart
    from duplicate-charge groups and individual orphan rows. The local snapshot
    is consistent; it cannot be atomic with a remote HTTP snapshot.
    """
    try:
        totals = fetch_payment_totals(api_url)
        residents, issues = read_reconciliation_snapshot()
        if set(totals) != {row["resident_id"] for row in residents}:
            raise ValueError("Partner resident roster does not match local residents")
        for row in residents:
            partner = totals[row["resident_id"]]
            delta = row["local_payments_cents"] - partner
            row.update(
                bigpms_payments_cents=partner,
                delta_cents=delta,
                status=(
                    "in_sync"
                    if delta == 0
                    else "missing_local_payments"
                    if delta < 0
                    else "local_exceeds_bigpms"
                ),
            )
    except (requests.RequestException, ValueError, psycopg2.Error):
        log.exception("Reconciliation unavailable: complete comparison could not be obtained")
        raise HTTPException(status_code=503, detail="Reconciliation unavailable") from None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "residents": residents,
        "issues": issues,
        "summary": {
            "residents_out_of_sync": sum(row["status"] != "in_sync" for row in residents),
            "issues_found": len(issues["duplicate_charges"])
            + len(issues["orphaned_ledger_entries"]),
        },
    }
