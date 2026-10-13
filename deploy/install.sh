#!/usr/bin/env bash
# One-shot setup on a fresh Ubuntu/Debian box. Run from anywhere.
#
# Verifies each step rather than trusting exit codes: `python3 -m venv` exits 0
# even when it fails to bootstrap pip, leaving a .venv with no pip and a
# confusing error on the following line.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=$(command -v python3)
PYVER=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "==> python $PYVER at $PY"

# --- 1. venv/pip support actually present? ---------------------------------
if ! "$PY" -c 'import ensurepip' 2>/dev/null; then
    echo "==> ensurepip missing, installing venv support"
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-venv "python${PYVER}-venv" 2>/dev/null \
        || sudo apt-get install -y -qq python3-venv
fi

# --- 2. venv, replacing a half-built one -----------------------------------
if [ -d .venv ] && [ ! -x .venv/bin/pip ]; then
    echo "==> removing incomplete .venv"
    rm -rf .venv
fi
if [ ! -d .venv ]; then
    echo "==> creating .venv"
    "$PY" -m venv .venv
fi

# --- 3. verify pip, repair if missing --------------------------------------
if [ ! -x .venv/bin/pip ]; then
    echo "==> bootstrapping pip inside .venv"
    .venv/bin/python -m ensurepip --upgrade 2>/dev/null || true
fi
if [ ! -x .venv/bin/pip ]; then
    cat >&2 <<'FAIL'

Could not build a working virtualenv.

Try, in order:
    sudo apt update
    sudo apt install -y python3-venv python3-pip
    rm -rf .venv
    ./deploy/install.sh

Still failing? Check disk space with `df -h .` — a full disk gives exactly
this symptom: skeleton created, pip bootstrap silently dropped.
FAIL
    exit 1
fi
echo "==> pip $(.venv/bin/pip --version | awk '{print $2}')"

# --- 4. dependencies -------------------------------------------------------
echo "==> installing requirements"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python -c 'import requests, yaml' \
    || { echo "dependency import failed" >&2; exit 1; }

# --- 5. env file -----------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> created .env from template — fill it in"
fi
chmod 600 .env

# --- 6. offline self-test --------------------------------------------------
echo "==> self-test (offline)"
.venv/bin/python run.py --source sample --mode buy --dry-run >/dev/null
.venv/bin/python run.py --source sample --mode broker --dry-run >/dev/null
rm -f radar.db
echo "    pipeline OK"

cat <<'NEXT'

Done. Next:

  1. Fill in .env        TG_TOKEN, TG_CHAT, optionally GEMINI_API_KEY
  2. config.yaml         paste your tutti search URL under searches: -> url
  3. Smoke test:
       set -a; source .env; set +a
       ./.venv/bin/python run.py --source sample --mode broker
     Four leads should land on your phone.
  4. Services (check User= and WorkingDirectory= in the unit files first):
       sudo cp deploy/*.service /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now willomotors-buy willomotors-listen
       journalctl -u willomotors-buy -f

NEXT
