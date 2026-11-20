# Security — secret handling

## ⚠️ Action required: rotate exposed credentials

`.env` was committed to git history (commits `4323609`, re-ignored at `6f281a9`). The committed
values are **byte-identical to the current ones** — they were never rotated and are still live.
Anyone with the repo history can recover them. `.env` is git-ignored again, so nothing re-commits
it, but **the secrets remain in history until it is rewritten.**

### 1. Rotate now (do this first — only you can, these are external accounts)
- **WhatsApp Cloud API token** — Meta → your app → WhatsApp → API Setup → generate a new token;
  update `SA_WHATSAPP_TOKEN` in your local `.env`.
- **Gmail app password** (`SA_SMTP_PASSWORD`) — Google Account → Security → App passwords →
  revoke the old one, create a new one; update `.env`.
- Also worth reviewing (lower risk, not secrets): `SA_WA_PHONE_NUMBER_ID`, `SA_SMTP_USER`,
  `SA_OTP_EMAIL_OVERRIDE`, `NGROK_DOMAIN`.

Rotation alone makes the leaked values useless. History rewriting (below) is cleanup; do it after
rotating and after coordinating with your teammate.

### 2. Purge `.env` from git history (coordinate first — rewrites shared history)

This rewrites every commit and requires a **force-push**. Your teammate must re-clone or hard-reset
afterwards, or they will re-introduce the old history on their next push. Agree a time, then:

**Option A — git-filter-repo (recommended):**
```bash
pip install git-filter-repo            # one-time
cd inbound-servicing-agent
git filter-repo --invert-paths --path .env     # removes .env from all history
git remote add origin https://github.com/pannagaUdup/inbound-servicing-agent.git  # filter-repo drops the remote
git push --force --all origin
git push --force --tags origin
```

**Option B — BFG Repo-Cleaner:**
```bash
bfg --delete-files .env
git reflog expire --expire=now --all && git gc --prune=now --aggressive
git push --force --all origin
```

**After the force-push**, every collaborator runs:
```bash
git fetch origin && git reset --hard origin/main     # or a fresh clone
```

> Not executed by tooling. Run these yourself after rotating the secrets and syncing with the team.

## Ongoing
- `.env` is in `.gitignore`; keep real secrets only there. `.env.example` holds placeholders.
- Never commit `.env`, tokens, or app passwords. CI/deploy should read them from the environment.
