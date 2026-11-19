"""Real document generation (owner: T2). Produces actual PDFs from LMS/D7 data.

Implements the DocumentGenerator protocol (receipt, statement) and adds foreclosure
quote + NOC. Keeps the same document_id scheme as the mock so handlers are unchanged;
also writes a real PDF to ``out_dir/{document_id}.pdf`` and returns its ``path``. The
outbound sender uploads that file to WhatsApp and sends it as a document attachment.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from fpdf import FPDF

from servicing_agent.data.store import DataStore

logger = logging.getLogger(__name__)


def _mask(account_id: str) -> str:
    return f"XXXX{account_id[-4:]}"


def _rs(v: Any) -> str:
    try:
        return f"Rs {float(v):,.2f}"
    except (TypeError, ValueError):
        return f"Rs {v}"


class PdfDocGen:
    def __init__(self, store: DataStore, out_dir: str | Path):
        self.s = store
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- helpers
    def _doc(self, title: str) -> FPDF:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(0, 96, 196)
        pdf.cell(0, 10, text="CreditNirvana", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, text="A Perfios Company", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
        pdf.set_text_color(37, 48, 93)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, text=title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 11)
        pdf.ln(2)
        return pdf

    def _kv(self, pdf: FPDF, label: str, value: str) -> None:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(55, 8, text=f"{label}:", new_x="RIGHT", new_y="TOP")
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 8, text=str(value), new_x="LMARGIN", new_y="NEXT")

    def _footer(self, pdf: FPDF) -> None:
        pdf.ln(6)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(120, 120, 120)
        pdf.multi_cell(0, 5, text=f"System-generated on {date.today().isoformat()}. "
                                  "For queries, message us on WhatsApp or call the helpline.")

    def _save(self, pdf: FPDF, document_id: str) -> str:
        path = self.dir / f"{document_id}.pdf"
        pdf.output(str(path))
        logger.info("DOC generated %s -> %s", document_id, path)
        return str(path)

    def path_for(self, document_id: str) -> str | None:
        p = self.dir / f"{document_id}.pdf"
        return str(p) if p.exists() else None

    # ---- documents
    def receipt(self, account_id: str, utr: str) -> dict[str, Any]:
        p = self.s.payments_by_utr[utr]
        assert p["account_id"] == account_id
        doc_id = f"RCPT-{utr}"
        pdf = self._doc("Payment Receipt")
        self._kv(pdf, "Loan account", _mask(account_id))
        self._kv(pdf, "Receipt no.", doc_id)
        self._kv(pdf, "Amount", _rs(p["amount"]))
        self._kv(pdf, "Payment date", p["txn_date"])
        self._kv(pdf, "UTR", utr)
        self._kv(pdf, "Channel", p.get("channel", "-"))
        self._kv(pdf, "Status", p.get("status", "-"))
        self._footer(pdf)
        return {"document_id": doc_id, "path": self._save(pdf, doc_id), "document_type": "receipt",
                "payment_date": p["txn_date"], "amount": p["amount"], "utr": utr}

    def statement(self, account_id: str, period_from: str, period_to: str) -> dict[str, Any]:
        doc_id = f"STMT-{account_id[-4:]}-{period_from}-{period_to}"
        pdf = self._doc("Statement of Account")
        self._kv(pdf, "Loan account", _mask(account_id))
        self._kv(pdf, "Period", f"{period_from} to {period_to}")
        pdf.ln(3)
        rows = sorted((p for p in self.s.payments_by_account.get(account_id, [])
                       if period_from <= p["txn_date"] <= period_to), key=lambda r: r["txn_date"])
        pdf.set_font("Helvetica", "B", 10)
        for h, w in [("Date", 35), ("Amount", 40), ("Channel", 30), ("UTR", 65)]:
            pdf.cell(w, 8, text=h, border=1, new_x="RIGHT", new_y="TOP")
        pdf.ln()
        pdf.set_font("Helvetica", "", 9)
        for p in rows:
            pdf.cell(35, 7, text=p["txn_date"], border=1, new_x="RIGHT", new_y="TOP")
            pdf.cell(40, 7, text=_rs(p["amount"]), border=1, new_x="RIGHT", new_y="TOP")
            pdf.cell(30, 7, text=p.get("channel", "-"), border=1, new_x="RIGHT", new_y="TOP")
            pdf.cell(65, 7, text=p["utr"], border=1, new_x="LMARGIN", new_y="NEXT")
        if not rows:
            pdf.cell(0, 7, text="No payments in this period.", new_x="LMARGIN", new_y="NEXT")
        self._footer(pdf)
        return {"document_id": doc_id, "path": self._save(pdf, doc_id), "document_type": "statement",
                "period_from": period_from, "period_to": period_to}

    def foreclosure_quote(self, account_id: str, quote: dict[str, Any]) -> dict[str, Any]:
        doc_id = f"FCQ-{account_id[-4:]}-{quote.get('valid_until', date.today().isoformat())}"
        pdf = self._doc("Foreclosure Quote")
        self._kv(pdf, "Loan account", _mask(account_id))
        self._kv(pdf, "Amount to close", _rs(quote.get("amount")))
        self._kv(pdf, "Valid until", quote.get("valid_until", "-"))
        b = quote.get("breakup", {})
        if b:
            pdf.ln(2)
            for k, v in b.items():
                self._kv(pdf, k.replace("_", " ").title(), _rs(v))
        self._footer(pdf)
        return {"document_id": doc_id, "path": self._save(pdf, doc_id), "document_type": "foreclosure_quote",
                "amount": quote.get("amount"), "valid_until": quote.get("valid_until")}

    def noc(self, account_id: str) -> dict[str, Any]:
        doc_id = f"NOC-{account_id[-4:]}"
        pdf = self._doc("No Objection Certificate")
        self._kv(pdf, "Loan account", _mask(account_id))
        pdf.ln(2)
        pdf.multi_cell(0, 6, text="This is to certify that the above loan account has been closed and "
                                  "there are no dues outstanding. CreditNirvana has no objection to the "
                                  "release of any security held against this loan.")
        self._footer(pdf)
        return {"document_id": doc_id, "path": self._save(pdf, doc_id), "document_type": "noc"}
