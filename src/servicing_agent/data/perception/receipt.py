"""Payment screenshot -> MediaEvidence via Sarvam Document AI.

Conversions come from real responses (Nov 2026):
* ``txn_time`` comes back as "9:45 pm" even when asked for HH:MM -> converted here.
* ``status`` comes back as the app's wording ("Completed") -> mapped to success/pending/failed.
* ``payee_vpa`` is often masked by the app ("....arvy@ptys"); reconciliation handles that.
* ``document_type`` confidence is unreliable (0.13 on a clear receipt), so "is this a
  receipt?" is decided here from a confident 12-digit UTR plus a confident amount.

Fields under the confidence threshold are not used; they're listed in
``low_confidence_fields`` so the agent asks the borrower to type the UTR instead of guessing.
"""

from __future__ import annotations

import re
from datetime import date

from servicing_agent.contracts.state import MediaEvidence
from servicing_agent.data.perception.sarvam import SarvamClient, SarvamError

CONFIDENCE_THRESHOLD = 0.9

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "description": "upi_receipt, bank_statement, id_document or other"},
        "utr": {"type": "string", "description": "12-digit UTR / RRN / UPI transaction ID"},
        "amount": {"type": "number", "description": "Amount paid in rupees"},
        "txn_date": {"type": "string", "description": "Transaction date, YYYY-MM-DD"},
        "txn_time": {"type": "string", "description": "Transaction time, HH:MM"},
        "payee_vpa": {"type": "string", "description": "Payee UPI ID, e.g. name@bank"},
        "payee_name": {"type": "string", "description": "Payee name as shown"},
        "status": {"type": "string", "description": "success, pending or failed as shown"},
    },
}

_STATUS = {
    "success": "success", "successful": "success", "completed": "success", "complete": "success",
    "paid": "success", "credited": "success",
    "pending": "pending", "processing": "pending", "in progress": "pending",
    "failed": "failed", "failure": "failed", "declined": "failed", "reversed": "failed",
}
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})(?::\d{2})?\s*([ap]\.?\s*m\.?)?\s*$", re.IGNORECASE)


def normalize_utr(value) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 12 else None


def normalize_amount(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    raw = re.sub(r"[^\d.]", "", str(value or ""))
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def normalize_date(value) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def normalize_time(value) -> str | None:
    m = _TIME_RE.match(str(value or ""))
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower().replace(".", "").replace(" ", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def normalize_status(value) -> str | None:
    return _STATUS.get(str(value or "").strip().lower())


def map_docai_result(body: dict, threshold: float = CONFIDENCE_THRESHOLD) -> MediaEvidence:
    """Pure mapping from a Document AI ``results`` response to MediaEvidence."""
    result = body.get("result") or {}
    ann = body.get("annotations") or {}

    def conf(field: str) -> float:
        c = (ann.get(field) or {}).get("confidence")
        return float(c) if isinstance(c, (int, float)) else 0.0

    raw_date = (ann.get("txn_date") or {}).get("normalized_value") or result.get("txn_date")
    values = {
        "utr": normalize_utr(result.get("utr")),
        "amount": normalize_amount(result.get("amount")),
        "txn_date": normalize_date(raw_date),
        "txn_time": normalize_time(result.get("txn_time")),
        "payee_vpa": (result.get("payee_vpa") or None),
        "payee_name": (result.get("payee_name") or None),
        "status": normalize_status(result.get("status")),
    }
    field_conf = {k: conf(k) for k in values if result.get(k) not in (None, "")}
    accepted, low = {}, []
    for k, v in values.items():
        if v is None:
            continue
        if field_conf.get(k, 0.0) >= threshold:
            accepted[k] = v
        else:
            low.append(k)

    is_receipt = "utr" in accepted and "amount" in accepted
    if not is_receipt and result.get("document_type") in ("bank_statement", "id_document") \
            and conf("document_type") >= threshold:
        kind = result["document_type"]
    else:
        kind = "upi_receipt" if (is_receipt or values["utr"] or values["amount"]) else "other"

    docs = body.get("documents") or []
    partial = any(d.get("partial") or d.get("failed_pages") for d in docs)
    return MediaEvidence(
        kind=kind,
        utr=accepted.get("utr"),
        amount=accepted.get("amount"),
        txn_date=accepted.get("txn_date"),
        txn_time=accepted.get("txn_time"),
        payee_vpa=accepted.get("payee_vpa"),
        payee_name=accepted.get("payee_name"),
        status=accepted.get("status"),
        language=((docs[0].get("language") or "").split("-")[0] or None) if docs else None,
        field_confidence=field_conf,
        low_confidence_fields=low,
        confidence=min(field_conf.get("utr", 0.0), field_conf.get("amount", 0.0)) if is_receipt else 0.0,
        provider="sarvam-docai",
        error="partial_extraction" if partial else None,
    )


class ReceiptExtractor:
    def __init__(self, client: SarvamClient, budget_s: float = 8.0, threshold: float = CONFIDENCE_THRESHOLD):
        self.client = client
        self.budget_s = budget_s
        self.threshold = threshold

    def extract(self, image: bytes, *, filename: str = "receipt.png", message_id: str | None = None) -> MediaEvidence:
        try:
            body = self.client.docai_extract(image, filename=filename, schema=RECEIPT_SCHEMA,
                                             budget_s=self.budget_s, idempotency_key=message_id)
        except SarvamError as exc:
            # The agent then asks for a typed UTR or a clearer image; D7 still decides payments.
            return MediaEvidence(kind="other", confidence=0.0, provider="sarvam-docai", error=f"docai_failed: {exc}")
        return map_docai_result(body, self.threshold)
