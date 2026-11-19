PY ?= .venv/bin/python

.PHONY: setup data run test eval

setup:
	python3 -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev,llm]"

data:
	$(PY) -m servicing_agent.data.generators.cli --out data/generated --accounts 10000 --seed 42

run:
	$(PY) -m uvicorn servicing_agent.platform.api.app:app --reload --port 8000

test:
	$(PY) -m pytest -q

eval:
	$(PY) -m evals.run_eval --data data/generated --seed 7 --n 300
