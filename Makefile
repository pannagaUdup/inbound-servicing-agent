PY ?= .venv/bin/python

.PHONY: setup data run run-local run-sarvam test eval db-up db-down db-load set-email reset-ladder

setup:
	uv venv
	uv pip install -e ".[dev,llm]"

db-up:
	docker compose up -d postgres redis

db-down:
	docker compose down

# Load data/generated CSVs into SA_DATABASE_URL (re-runnable: replaces the source-system tables)
db-load:
	$(PY) -m servicing_agent.data.db_loader

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

# Email verification codes for a WhatsApp number to a real inbox: make set-email MOBILE=8867635479 EMAIL=you@x.com
set-email:
	$(PY) -m servicing_agent.platform.contact_email --mobile $(MOBILE) --email $(EMAIL)

# Demo: forget the hardship ladder (encouragement rounds, offers) for one loan, or all: make reset-ladder [ACCOUNT=CN...]
reset-ladder:
	$(PY) -c "from dotenv import load_dotenv; load_dotenv('.env'); import redis; from servicing_agent.config import get_settings; r = redis.from_url(get_settings().redis_url); keys = list(r.scan_iter('sa:ctx:state:hardship:$(or $(ACCOUNT),*)')); [r.delete(k) for k in keys]; print('cleared', len(keys), 'ladder state(s)')"
