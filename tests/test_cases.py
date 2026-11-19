import threading

import pytest

from servicing_agent.contracts.cases import CaseRequest, CaseStatus, CaseType, DecisionIn
from servicing_agent.platform.cases import Conflict, is_material_change


def _req(amount, **kw):
    return CaseRequest(type=CaseType.WAIVER, amount=amount, components=["penal"], **kw)


def test_material_change_detection():
    base = _req(5000).model_dump(mode="json")
    assert is_material_change(base, _req(3000).model_dump(mode="json"))
    assert not is_material_change(base, _req(5000).model_dump(mode="json"))
    assert is_material_change(base, _req(5000, plan="instalments").model_dump(mode="json"))


def test_supersede_cancels_old_and_restarts(runtime):
    cases = runtime.cases
    v1 = cases.create("CN0000000001", "conv", _req(5000))
    v2 = cases.supersede(v1.case_id, 1, _req(3000), reason="changed")
    assert v2.version == 2 and v2.status == CaseStatus.PENDING_APPROVER
    assert v2.diff == {"amount": {"from": 5000.0, "to": 3000.0}}
    old = cases.get_version(v1.case_id, 1)
    assert old.status == CaseStatus.SUPERSEDED and old.superseded_by == 2
    # decision on the cancelled version is rejected
    with pytest.raises(Conflict):
        cases.decide(v1.case_id, 1, DecisionIn(role="approver", decision="approve", rationale="ok",
                                               reviewer_id="r1", etag=v1.etag))


def test_full_approval_chain_and_stale_etag(runtime):
    cases = runtime.cases
    v1 = cases.create("CN0000000002", "conv", _req(2000))
    with pytest.raises(Conflict):
        cases.decide(v1.case_id, 1, DecisionIn(role="compliance", decision="approve", rationale="skip",
                                               reviewer_id="c1", etag=v1.etag))
    a = cases.decide(v1.case_id, 1, DecisionIn(role="approver", decision="approve", rationale="ok",
                                               reviewer_id="r1", etag=v1.etag))
    assert a.status == CaseStatus.PENDING_COMPLIANCE
    with pytest.raises(Conflict):  # stale etag
        cases.decide(v1.case_id, 1, DecisionIn(role="compliance", decision="approve", rationale="ok",
                                               reviewer_id="c1", etag=v1.etag))
    c = cases.decide(v1.case_id, 1, DecisionIn(role="compliance", decision="approve", rationale="ok",
                                               reviewer_id="c1", etag=a.etag))
    assert c.status == CaseStatus.APPROVED


def test_concurrent_decision_vs_supersede_never_approves_cancelled_version(runtime):
    """Approval accepted on a cancelled version must be 0."""
    cases = runtime.cases
    bad = 0
    for i in range(200):
        v1 = cases.create(f"CN{i:010d}", "conv", _req(5000))
        results = {}

        def approve():
            try:
                results["approve"] = cases.decide(v1.case_id, 1, DecisionIn(
                    role="approver", decision="approve", rationale="ok", reviewer_id="r", etag=v1.etag))
            except Conflict:
                results["approve"] = None

        def supersede():
            try:
                results["supersede"] = cases.supersede(v1.case_id, 1, _req(3000), reason="changed")
            except Conflict:
                results["supersede"] = None

        ts = [threading.Thread(target=approve), threading.Thread(target=supersede)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        final_v1 = cases.get_version(v1.case_id, 1)
        approved, superseded = results["approve"] is not None, results["supersede"] is not None
        # Approve-then-supersede both succeed (the change cancels an in-flight approval).
        # Supersede-then-approve must reject the approval. So:
        if superseded and final_v1.status != CaseStatus.SUPERSEDED:
            bad += 1          # cancel lost, or an approval landed after it
        if not approved and not superseded:
            bad += 1          # nobody won
        if approved and not superseded and final_v1.status != CaseStatus.PENDING_COMPLIANCE:
            bad += 1
    assert bad == 0


def test_approval_after_supersede_is_rejected(runtime):
    cases = runtime.cases
    v1 = cases.create("CN9999999999", "conv", _req(5000))
    cases.supersede(v1.case_id, 1, _req(3000), reason="changed")
    with pytest.raises(Conflict):
        cases.decide(v1.case_id, 1, DecisionIn(role="approver", decision="approve", rationale="late",
                                               reviewer_id="r", etag=cases.get_version(v1.case_id, 1).etag))
