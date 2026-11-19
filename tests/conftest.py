from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from servicing_agent.config import get_settings
from servicing_agent.data.generators.core import generate, write_csvs


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("data")
    write_csvs(generate(n_accounts=600, seed=11), d)
    return d


@pytest.fixture
def settings(data_dir, tmp_path):
    return replace(get_settings(), data_dir=data_dir, database_url=f"sqlite:///{tmp_path}/test.db",
                   otp_stub_code="123456", otp_enabled=True, planner="rules")


@pytest.fixture
def runtime(settings):
    from servicing_agent.platform.runtime import build_runtime
    return build_runtime(settings)


@pytest.fixture
def borrower(runtime):
    """(account, sender) for an account with a successful payment and an active borrower contact."""
    s = runtime.store
    for a in s.accounts.values():
        if not any(p["status"] == "success" for p in s.payments_by_account.get(a["account_id"], [])):
            continue
        for link in s.links_by_account[a["account_id"]]:
            ct = s.contacts[link["contact_id"]]
            if link["role"] == "borrower" and ct["is_active"]:
                return a, "91" + ct["mobile"]
    raise RuntimeError("no suitable borrower")
