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
