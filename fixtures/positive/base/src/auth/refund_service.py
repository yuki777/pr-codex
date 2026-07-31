from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Actor:
    tenant_id: str


@dataclass(frozen=True)
class Payment:
    # Payment ids are only unique inside a tenant.
    id: str
    tenant_id: str
    amount: Decimal
    state: str


class PaymentRepository(Protocol):
    def get_many(self, payment_ids: Sequence[str]) -> list[Payment]: ...

    def record_refund(
        self,
        payment: Payment,
        amount: Decimal,
        idempotency_key: str,
    ) -> str:
        """Record a refund. Idempotency keys share one global namespace."""
        ...


class PaymentService:
    def __init__(self, repository: PaymentRepository) -> None:
        self._repository = repository

    def refund_one(
        self,
        actor: Actor,
        payment_id: str,
        idempotency_key: str,
    ) -> str:
        payment = self._repository.get_many([payment_id])[0]
        if payment.tenant_id != actor.tenant_id:
            raise PermissionError("payment belongs to another tenant")
        if payment.state != "captured":
            raise ValueError("only captured payments can be refunded")

        repository_key = f"{actor.tenant_id}:{idempotency_key}:{payment.id}"
        return self._repository.record_refund(
            payment,
            payment.amount,
            repository_key,
        )
