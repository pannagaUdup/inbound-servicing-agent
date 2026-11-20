"""Compare chat models on the agent's real model tasks (Sarvam /v2/chat/completions).

    .venv/bin/python -m evals.compare_models [--models sarvam-105b-conversations,deepseekv4.1-flash-1017] [--repeat 1]

Each model runs alone (no fallback) through the agent's own components, so the numbers are what the agent
would get with that model first:

1. intent    - SarvamIntentClassifier on short, messy, mixed-language messages (accepted label sets)
2. translate - SarvamNormalizer on code-mixed / Indic text, then the rules planner; scores: right intent
               after translation, identifiers kept, no numbers invented
3. answer    - SarvamAnswerer on loan questions; scores: expected fact present and output guard passes
4. rewrite   - ReplyWriter on approved replies (English + Hindi); score: rewrite accepted by the fact checks

Prints a table and writes evals/results/model_compare.json. Costs one API call per item per model.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from dotenv import load_dotenv

INTENT = [
    ("will this spoil my record with banks", {"policy_question"}),
    ("who is handling my recovery", {"restricted_info"}),
    ("can u tell wen my loan gets over", {"account_question", "loan_summary"}),
    ("i want to close everything at once", {"foreclosure_quote"}),
    ("the money got cut twice from my account", {"dispute", "charge_reversal", "complaint"}),
    ("is there any way to reduce what I owe, lost my job", {"settlement_request", "waiver_request"}),
    ("remove the late fee please", {"waiver_request"}),
    ("pls send last 6 month statment", {"statement_request"}),
    ("how much do i have to pay this month", {"emi_due_query"}),
    ("send me the link to pay", {"pay_now"}),
    ("whats the update on my waiver", {"case_status"}),
    ("did my payment from yesterday reach you", {"payment_claim"}),
    ("whats the weather in bangalore", {"out_of_scope"}),
    ("who decides if my settlement is approved", {"policy_question"}),
    ("what is my rate of intrest", {"loan_summary"}),
    ("how many emi r pending", {"loan_summary"}),
    ("i never took this loan", {"dispute"}),
    ("your staff was very rude to me", {"complaint"}),
    ("what are bounce charges", {"charges_query"}),
    ("branch timings?", {"faq"}),
    ("nanna loan eshtu baki ide", {"loan_summary", "emi_due_query", "account_question"}),
    ("EMI kitna hai", {"emi_due_query"}),
    ("maine payment kar diya hai", {"payment_claim"}),
    ("can I get my NOC", {"account_question", "statement_request", "receipt_request", "unknown"}),
]

# Real messages from the 2026-11-20 WhatsApp sessions (voice transcripts and texts), with the labels a person
# would give them. These are the "deviation" questions: about the account, the waiver / settlement policy,
# complaints and earlier requests, phrased the way borrowers actually speak.
REAL = [
    ("What is my total outstanding which needs to be paid?", {"loan_summary", "account_question", "emi_due_query"}),
    ("When is the final date for me to make this payment?", {"emi_due_query", "account_question"}),
    ("What if I won't be able to make the full payment on time?", {"payment_difficulty", "policy_question"}),
    ("I won't be able to make full payment right now.", {"payment_difficulty"}),
    ("I can only pay ₹5000 max.", {"payment_difficulty", "pay_now"}),
    ("I won't be able to pay much more further, only ₹5000. I don't have much money for that.", {"payment_difficulty"}),
    ("Why does the waiver and settlement need approval?", {"policy_question"}),
    ("Yes, I would like to request a settlement.", {"settlement_request"}),
    ("Tell me more about my previous request.", {"case_status"}),
    ("Terminate the whole thing and reinitiate a new flow for ₹1615.", {"case_withdraw", "waiver_request", "settlement_request"}),
    ("What is my current outstanding amount and EMI to be paid this month?", {"loan_summary", "emi_due_query", "account_question"}),
    ("Uh, this discrepancy is causing too much of problems. Can you escalate this one to the compliance team?",
     {"escalation_request"}),
    ("What is the UTR number of the one which I sent?", {"account_question"}),
    ("I have already paid but you people keep calling me, this is harassment", {"harassment_complaint", "complaint"}),
    ("the late fee you charged me is wrong, reverse it", {"charge_reversal", "dispute"}),
    ("can the bank reduce my interest if I pay 3 emis together", {"policy_question", "account_question", "loan_summary"}),
    ("if I settle will it show in my cibil", {"policy_question"}),
    ("my salary is delayed, can you give me some time till 15th", {"payment_difficulty"}),
]

TRANSLATE = [
    ("naanu ninne 10 saavira kattidini, UTR 512398765432", "payment_claim", ["512398765432"]),
    ("maine kal 1481 rupay bhar diya", "payment_claim", []),
    ("mera EMI kitna hai is mahine", "emi_due_query", []),
    ("statement kalisi please", "statement_request", []),
    ("penalty maaf kar do please", "waiver_request", []),
    ("loan close karna hai, kitna dena padega", "foreclosure_quote", []),
    ("मेरी रसीद भेजो", "receipt_request", []),
    ("ನನ್ನ ಸ್ಟೇಟ್ಮೆಂಟ್ ಕಳುಹಿಸಿ", "statement_request", []),
    ("என் EMI எவ்வளவு", "emi_due_query", []),
    ("maine UTR 256189464343 se payment kiya tha, check karo", "payment_claim", ["256189464343"]),
]

FACTS = {"product": "personal", "sanction_amount": 44300.0, "principal_outstanding": 922.92, "interest_rate": 25.53,
         "emi_amount": 1481.99, "tenure_months": 48, "tenure_left": 1, "next_due_date": "2026-12-05",
         "overdue_amount": 11855.92, "bounce_charges": 3540.0, "penal_charges": 0.0, "status": "active"}
ANSWER = [
    ("when does my loan end", ["2026-12-05"]),
    ("what was my loan amount", ["44,300"]),
    ("how many emis have i paid", ["47"]),
    ("is my loan active", ["active"]),
    ("how much is overdue", ["11,855.92"]),
    ("what are my bounce charges", ["3,540"]),
]

REWRITE = [
    ("en", "claim_failed_escalated", dict(detail=" of ₹1,481.99 (UTR ending 5499)", window="2 minutes", ticket_id="TKT000002")),
    ("hi", "claim_checking", dict(detail=" of ₹1,481.99 (UTR ending 5499)", window="2 minutes")),
    ("en", "claim_other_own_loan", dict(detail=" of ₹11,855.92 (UTR ending 5440)", other="CN92••••5532", current="CN89••••4971")),
    ("hi", "case_not_needed", dict(paid="11,855.92", due="11,855.92", verb="waive")),
    ("en", "waiver_created", dict(kind="waiver", case_id="CASEF7F8B93267")),
    ("kn", "claim_matched", dict(amount="1,481.99", payment_date="2026-11-19", utr="512398765432")),
]


def _timed(fn):
    t = time.monotonic()
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        out = exc
    return out, time.monotonic() - t


ALL_TASKS = ("intent", "real", "translate", "answer", "rewrite")


def rules_baseline(cases) -> dict:
    """What the keyword rules alone say (they run before any model in the live agent)."""
    from servicing_agent.agent.planner import RulePlanner
    rp, items = RulePlanner(), []
    for text, ok in cases:
        labels = [t.intent.value for t in rp.plan(text)]
        hit = bool(labels) and labels[0] != "unknown"
        items.append({"text": text, "got": labels, "pass": hit and labels[0] in ok, "rules_matched": hit, "s": 0.0})
    return items


def run(models: list[str], repeat: int = 1, tasks: tuple = ALL_TASKS) -> dict:
    from servicing_agent.agent.answerer import SarvamAnswerer
    from servicing_agent.agent.normalizer import SarvamNormalizer
    from servicing_agent.agent.output_guard import check
    from servicing_agent.agent.planner import RulePlanner, SarvamIntentClassifier
    from servicing_agent.agent.reply_writer import ReplyWriter
    from servicing_agent.agent.templates import render
    from servicing_agent.data.perception.sarvam import SarvamClient, SarvamConfig

    client = SarvamClient(SarvamConfig.from_env())
    rules = RulePlanner()
    allowed = {round(float(v), 2) for v in FACTS.values() if isinstance(v, (int, float))} | {47.0}
    report: dict = {}
    for model in models:
        r = {"intent": [], "real": [], "translate": [], "answer": [], "rewrite": []}
        clf = SarvamIntentClassifier(client, [model])
        norm = SarvamNormalizer(client, model=model, fallback_model=None)
        ans = SarvamAnswerer(client, [model])
        wr = ReplyWriter(client, [model])
        for _ in range(repeat):
            for text, ok in (REAL if "real" in tasks else []):
                out, dt = _timed(lambda: clf.classify(text))
                labels = [] if isinstance(out, Exception) else [i.value for i in out]
                r["real"].append({"text": text, "got": labels, "pass": bool(labels) and labels[0] in ok, "s": dt})
            for text, ok in (INTENT if "intent" in tasks else []):
                out, dt = _timed(lambda: clf.classify(text))
                labels = [] if isinstance(out, Exception) else [i.value for i in out]
                r["intent"].append({"text": text, "got": labels, "pass": bool(labels) and labels[0] in ok, "s": dt})
            for text, gold, ids in (TRANSLATE if "translate" in tasks else []):
                out, dt = _timed(lambda: norm.translate(text))
                if isinstance(out, Exception) or not out.ok:
                    r["translate"].append({"text": text, "pass": False, "ids_kept": False, "s": dt, "error": str(out)})
                    continue
                intents = [t.intent.value for t in rules.plan(out.english)]
                ids_kept = all(i in out.english for i in ids)
                r["translate"].append({"text": text, "english": out.english, "intents": intents, "ids_kept": ids_kept,
                                       "invented_numbers": out.removed_numbers,
                                       "pass": gold in intents and ids_kept and not out.removed_numbers, "s": dt})
            for q, expect in (ANSWER if "answer" in tasks else []):
                out, dt = _timed(lambda: ans.answer(q, FACTS))
                text = "" if isinstance(out, Exception) or not out else out
                guard = check(text, verified_account="CN9248665532", allowed_numbers=allowed,
                              allowed_strings={"2026-12-05", "personal", "active"}).ok if text else False
                r["answer"].append({"q": q, "answer": text, "guard_ok": guard,
                                    "pass": guard and all(e in text for e in expect), "s": dt})
            for lang, tpl, kw in (REWRITE if "rewrite" in tasks else []):
                original = render(tpl, **kw).text
                out, dt = _timed(lambda: wr.rewrite(original, language=lang))
                r["rewrite"].append({"lang": lang, "template": tpl, "out": None if isinstance(out, Exception) else out,
                                     "pass": isinstance(out, str) and bool(out), "s": dt})
        report[model] = {k: v for k, v in r.items() if v}
    report["rules-only"] = {k: rules_baseline(c) for k, c in (("intent", INTENT), ("real", REAL)) if k in tasks}
    return report


def summarize(report: dict) -> list[dict]:
    rows = []
    for model, r in report.items():
        row = {"model": model}
        for task, items in r.items():
            row[f"{task}_acc"] = sum(i["pass"] for i in items) / len(items)
            row[f"{task}_p50_s"] = statistics.median(i["s"] for i in items)
        if r.get("translate"):
            row["translate_ids_kept"] = sum(i.get("ids_kept", False) for i in r["translate"]) / len(r["translate"])
        if r.get("answer"):
            row["answer_guard_ok"] = sum(i["guard_ok"] for i in r["answer"]) / len(r["answer"])
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    load_dotenv(".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="sarvam-105b-conversations,deepseekv4.1-flash-1017")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--tasks", default=",".join(ALL_TASKS), help="comma list of: " + ", ".join(ALL_TASKS))
    a = ap.parse_args(argv)
    tasks = tuple(t.strip() for t in a.tasks.split(",") if t.strip())
    report = run(a.models.split(","), a.repeat, tasks)
    rows = summarize(report)
    out = Path("evals/results/model_compare.json")
    out.write_text(json.dumps({"summary": rows, "detail": report}, indent=2, ensure_ascii=False, default=str))
    print(f"{'model':28} " + " ".join(f"{t + ' acc':>14} {'p50 s':>6}" for t in tasks))
    for row in rows:
        print(f"{row['model']:28} " + " ".join(
            f"{row[t + '_acc']:>14.0%} {row[t + '_p50_s']:>6.2f}" if t + "_acc" in row else f"{'-':>14} {'':>6}"
            for t in tasks))
    print(f"\ndetail: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
