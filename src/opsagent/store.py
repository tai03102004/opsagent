"""In-memory data access over the seed JSON. Mutated only by the executor."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Customer, Order, Payment, Subscription


class Store:
    def __init__(
        self,
        customers: list[Customer],
        orders: list[Order],
        payments: list[Payment],
        subscriptions: list[Subscription],
        now: datetime,
    ):
        self.now = now
        self._customers = {c.id: c for c in customers}
        self._orders = {o.id: o for o in orders}
        self._payments = {p.id: p for p in payments}
        self._subscriptions = {s.id: s for s in subscriptions}

    @classmethod
    def load(cls, data_dir: Path, now: datetime) -> "Store":
        def read(name: str) -> list[dict]:
            return json.loads((Path(data_dir) / f"{name}.json").read_text(encoding="utf-8"))

        return cls(
            customers=[Customer(**r) for r in read("customers")],
            orders=[Order(**r) for r in read("orders")],
            payments=[Payment(**r) for r in read("payments")],
            subscriptions=[Subscription(**r) for r in read("subscriptions")],
            now=now,
        )

    # ------------------------------------------------------------ reads
    def customers(self) -> list[Customer]:
        return list(self._customers.values())

    def orders(self) -> list[Order]:
        return list(self._orders.values())

    def subscriptions(self) -> list[Subscription]:
        return list(self._subscriptions.values())

    def customer(self, customer_id: str) -> Optional[Customer]:
        return self._customers.get(customer_id)

    def customer_by_email(self, email: Optional[str]) -> Optional[Customer]:
        if not email:
            return None
        wanted = email.strip().lower()
        return next((c for c in self._customers.values() if c.email.lower() == wanted), None)

    def order(self, order_id: Optional[str]) -> Optional[Order]:
        if not order_id:
            return None
        return self._orders.get(order_id.strip().upper())

    def payment(self, payment_id: str) -> Optional[Payment]:
        return self._payments.get(payment_id)

    def payments_for(self, order_id: str) -> list[Payment]:
        return sorted(
            (p for p in self._payments.values() if p.order_id == order_id),
            key=lambda p: p.created_at,
        )

    def subscriptions_for(self, customer_id: str) -> list[Subscription]:
        return [s for s in self._subscriptions.values() if s.customer_id == customer_id]

    # ------------------------------------------------------------ writes (executor only)
    def mark_refunded(self, order_id: str, payment_id: Optional[str] = None) -> None:
        if payment_id:
            self._payments[payment_id].status = "refunded"
        else:
            self._orders[order_id].status = "refunded"

    def mark_subscription_cancelled(self, subscription_id: str) -> None:
        self._subscriptions[subscription_id].status = "cancelled"
