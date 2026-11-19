PY ?= .venv/bin/python

.PHONY: setup data run run-sarvam test eval
.PHONY: setup data run test eval db-up db-down

setup:
	python3 -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev,llm]"

db-up:
	docker compose up -d postgres redis

db-down:
	docker compose down

data:
	$(PY) -m servicing_agent.data.generators.cli --out data/generated --accounts 10000 --seed 42

run:
	$(PY) -m uvicorn servicing_agent.platform.api.app:app --reload --port 8000

# Real images and voice notes via Sarvam (needs SARVAM_API_KEY in .env); media saved under data/media/
run-sarvam:
	SA_PERCEPTION=sarvam $(PY) -m uvicorn servicing_agent.platform.api.app:app --reload --port 8000

test:
	$(PY) -m pytest -q

eval:
	$(PY) -m evals.run_eval --data data/generated --seed 7 --n 300
