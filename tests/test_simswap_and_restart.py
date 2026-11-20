"""SIM-swap check (SEC-01) before documents and waivers; a restarted request retires the old version's tickets."""

from datetime import date

from tests.test_agent_flow import say, verify


def _new_number(runtime, borrower, monkeypatch):
    """Make the borrower's mobile look recently added to the loan (restored after the test: the store is cached)."""
    a, sender = borrower
    c = runtime.tools.lms.active_contact_for(a["account_id"], sender)
    monkeypatch.setitem(runtime.store.contacts[c["contact_id"]], "added_on", date.today().isoformat())
    return a, sender


def test_documents_wait_for_a_security_check_on_a_new_number(runtime, borrower, monkeypatch):
    a, sender = _new_number(runtime, borrower, monkeypatch)
    verify(runtime, a, sender)
    r = say(runtime, sender, "send my statement")
    assert r.replies[0].template_id == "sim_swap_hold" and not r.replies[0].attachments
    assert r.tickets[0]["request_type"] == "sim_swap_review" and r.tickets[0]["queue"] == "security"
    ticket = r.tickets[0]["ticket_id"]
    r = say(runtime, sender, "send me the foreclosure quote")
    assert r.replies[0].template_id == "sim_swap_hold" and ticket in r.replies[0].text and not r.tickets


def test_loan_answers_are_not_held(runtime, borrower, monkeypatch):
    a, sender = _new_number(runtime, borrower, monkeypatch)
    verify(runtime, a, sender)
    assert say(runtime, sender, "what is my emi").replies[0].template_id == "emi_due"


def test_waiver_on_a_new_number_is_flagged_for_the_approver(runtime, borrower, monkeypatch):
    a, sender = _new_number(runtime, borrower, monkeypatch)
    verify(runtime, a, sender)
    say(runtime, sender, "please waive my penal charges of Rs 5000, my salary was delayed")
    r = say(runtime, sender, "yes")
    assert [x.template_id for x in r.replies] == ["waiver_created", "sim_swap_case_note"]
    approval = next(t for t in r.tickets if t["request_type"] == "waiver_request")
    security = next(t for t in r.tickets if t["request_type"] == "sim_swap_review")
    assert approval["summary"]["security_check"]["ticket_id"] == security["ticket_id"]


def test_an_established_number_is_unaffected(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "send my statement")
    assert r.replies[0].template_id == "statement_sent" and r.replies[0].attachments


def test_restart_retires_the_old_version_tickets_and_reopens_approval(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "please waive my penal charges of Rs 5000, my salary was delayed")
    first = say(runtime, sender, "yes").tickets[0]
    say(runtime, sender, "can you make the waiver Rs 3000")
    r = say(runtime, sender, "yes")
    assert r.replies[0].template_id == "waiver_restarted"
    new = r.tickets[0]
    assert new["request_type"] == first["request_type"] and new["summary"]["version"] == 2
    assert new["summary"]["supersedes"] == [first["ticket_id"]] and new["summary"]["diff"]
    assert runtime.tools.tickets.status_of(first["ticket_id"]) == "superseded"
    assert runtime.tools.tickets.open_for_account(a["account_id"], first["request_type"])["ticket_id"] == new["ticket_id"]
