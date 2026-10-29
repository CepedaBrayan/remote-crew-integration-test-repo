from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class PaymentData(BaseModel):
    """Validate ledger fields while retaining partner metadata for auditing."""

    model_config = ConfigDict(extra="allow")
    resident_id: str = Field(min_length=1)
    amount_cents: int = Field(strict=True, gt=0, le=2_147_483_647)


class PaymentEvent(BaseModel):
    """A payment event with an aware timestamp and preserved extra JSON fields."""

    model_config = ConfigDict(extra="allow")
    event_id: str = Field(min_length=1)
    event_type: Literal["payment.received"]
    occurred_at: AwareDatetime
    data: PaymentData


class FeedEvent(PaymentData):
    """Validate the partner's flat feed representation before any SQL writes."""

    seq: int = Field(strict=True, gt=0, le=9_223_372_036_854_775_807)
    event_id: str = Field(min_length=1)
    event_type: Literal["payment.received"]
    occurred_at: AwareDatetime

    def as_payment(self) -> PaymentEvent:
        """Normalize a validated feed row to the shared payment insertion contract."""
        return PaymentEvent(
            event_id=self.event_id,
            event_type=self.event_type,
            occurred_at=self.occurred_at,
            data=PaymentData(resident_id=self.resident_id, amount_cents=self.amount_cents),
        )


class FeedPage(BaseModel):
    """Require complete paging metadata and valid events; never silently skip bad data."""

    events: list[FeedEvent]
    next_cursor: int = Field(strict=True, ge=0, le=9_223_372_036_854_775_807)
    has_more: bool = Field(strict=True)

    def check_progress(self, cursor: int) -> None:
        """Reject cursor regression, unexplained advancement and non-progressing pages.

        BigPMS defines next_cursor as the maximum sequence returned, or the input
        cursor for an empty terminal page. Duplicate rows are allowed. Sequence
        gaps are not interpreted as missing rows because the feed owns ordering.
        """
        expected = max((event.seq for event in self.events), default=cursor)
        if self.next_cursor != expected or any(event.seq <= cursor for event in self.events):
            raise ValueError("Feed cursor does not match returned event sequences")
        if self.has_more and self.next_cursor <= cursor:
            raise ValueError("Feed has_more page made no progress")
