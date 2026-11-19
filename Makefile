PY ?= .venv/bin/python

.PHONY: setup data run run-local run-sarvam test eval db-up db-down

setup:
	uv venv
	uv pip install -e ".[dev,llm]"

db-up:
	docker compose up -d postgres redis

db-down:
	docker compose down

data:
	$(PY) -m servicing_agent.data.generators.cli --out data/generated --accounts 10000 --seed 42

# Starts the server AND a cloudflared tunnel; prints the Meta callback URL. (Set CF= to point at the binary.)
run:
	bash scripts/run.sh

# Server + tunnel without the tunnel (plain uvicorn), if you don't want a public URL.
run-local:
	$(PY) -m uvicorn servicing_agent.platform.api.app:app --reload --port 8000

# Real images and voice notes via Sarvam (needs SARVAM_API_KEY in .env); media saved under data/media/
run-sarvam:
	SA_PERCEPTION=sarvam bash scripts/run.sh

test:
	$(PY) -m pytest -q

eval:
	$(PY) -m evals.run_eval --data data/generated --seed 7 --n 300
