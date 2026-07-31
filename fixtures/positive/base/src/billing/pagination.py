from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class PaymentRow:
    id: str
    created_at: datetime


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    payment_id: str


@dataclass(frozen=True)
class Page:
    items: list[PaymentRow]
    next_cursor: str | None


class PaymentQuery(Protocol):
    def page_before(
        self,
        tenant_id: str,
        cursor: Cursor | None,
        limit: int,
    ) -> list[PaymentRow]:
        """Return rows ordered by (created_at DESC, id DESC)."""
        ...


def encode_cursor(cursor: Cursor) -> str:
    return f"{cursor.created_at.isoformat()}|{cursor.payment_id}"


def decode_cursor(value: str) -> Cursor:
    created_at, payment_id = value.rsplit("|", 1)
    return Cursor(datetime.fromisoformat(created_at), payment_id)


def list_recent(
    query: PaymentQuery,
    tenant_id: str,
    after: str | None,
    limit: int,
) -> Page:
    cursor = decode_cursor(after) if after else None
    rows = query.page_before(tenant_id, cursor, limit + 1)
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page_rows[-1]
        next_cursor = encode_cursor(Cursor(last.created_at, last.id))
    return Page(page_rows, next_cursor)
