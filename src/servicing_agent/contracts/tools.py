"""Tool signatures the agent may call (owner: T2).

Every account-scoped method takes ``account_id`` as its first argument, but the
agent never supplies it: the policy gateway injects ``scope.account_id``.
Mock implementations live in ``platform/tools_mock.py``; real integrations must
keep these exact signatures.
"""

from __future__ import annotations

from typing import Any, Protocol


class LMS(Protocol):
    def account_exists(self, account_id: str) -> bool: ...
    def active_contact_for(self, account_id: str, mobile: str) -> dict[str, Any] | None:
        """Return {contact_id, customer_id, role, added_on} if ``mobile`` is an active contact on the account."""
    def dues(self, account_id: str) -> dict[str, Any]: ...
    def foreclosure_quote(self, account_id: str) -> dict[str, Any]: ...
    def loan_position(self, account_id: str) -> dict[str, Any]: ...


class PaymentFeed(Protocol):
    def find_by_utr(self, utr: str) -> dict[str, Any] | None: ...
    def find_candidates(self, account_id: str, amount: float, date_iso: str, days: int = 1) -> list[dict[str, Any]]: ...
    def history(self, account_id: str, months: int = 12) -> list[dict[str, Any]]: ...
    def is_claimed(self, utr: str) -> bool: ...
    def mark_claimed(self, utr: str, account_id: str) -> None: ...


class OTPService(Protocol):
    def send(self, mobile: str, account_id: str | None = None) -> str | None:
        """Send an OTP and return a request id (None if none could be issued). Never returns the code.

        ``account_id`` (Nov 2026, optional) lets a real service deliver to that account's
        registered channel (e.g. email)."""
    def verify(self, request_id: str, code: str) -> bool: ...


class DocumentGenerator(Protocol):
    def receipt(self, account_id: str, utr: str) -> dict[str, Any]: ...
    def statement(self, account_id: str, period_from: str, period_to: str) -> dict[str, Any]: ...


class PaymentLinks(Protocol):
    def create(self, account_id: str, amount: float) -> dict[str, Any]: ...


class Ticketing(Protocol):
    def create(self, request_type: str, summary: dict[str, Any], conversation_id: str,
               account_id: str | None = None) -> dict[str, Any]: ...


class ToolSet(Protocol):
    lms: LMS
    payments: PaymentFeed
    otp: OTPService
    docs: DocumentGenerator
    paylinks: PaymentLinks
    tickets: Ticketing
