from servicing_agent.data.generators.core import generate, validate
from servicing_agent.data.identifiers import is_valid_account_number, make_account_number, normalize_mobile


def test_account_number_check_digit():
    acc = make_account_number("123456789")
    assert is_valid_account_number(acc)
    bad = acc[:-1] + str((int(acc[-1]) + 1) % 10)
    assert not is_valid_account_number(bad)
    assert not is_valid_account_number("CN12345")


def test_normalize_mobile():
    assert normalize_mobile("+91 98765-43210") == "9876543210"
    assert normalize_mobile("919876543210") == "9876543210"


def test_generation_is_deterministic_and_valid():
    a, b = generate(300, seed=5), generate(300, seed=5)
    assert [x["account_id"] for x in a.accounts] == [x["account_id"] for x in b.accounts]
    assert validate(a) == []
    assert all(is_valid_account_number(x["account_id"]) for x in a.accounts)
    assert len(a.accounts) == 300
    # some customers hold several accounts
    assert len({x["customer_id"] for x in a.accounts}) < 300
