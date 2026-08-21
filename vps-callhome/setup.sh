#!/bin/bash
cd /root/vps-callhome
python3 -m venv .venv
source .venv/bin/activate
pip install -q fastapi uvicorn python-multipart httpx pydantic-settings
echo "DEPS_DONE"
# Copy .env
cp .env .env
echo "SETUP_DONE"
