"""Durable delivery envelopes shared by Outbox publishers and Inbox consumers."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OutboxDeliveryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: str = Field(min_length=1, max_length=40)
    message_id: str = Field(min_length=1, max_length=40)
    topic: str = Field(min_length=1, max_length=80)
    attempt: int = Field(ge=1)
    attempted_at: datetime
    payload: dict[str, object]


class InboxConsumeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    processed: bool
    duplicate: bool
    inbox_id: str
