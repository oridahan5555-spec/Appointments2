@echo off
set ALLOWED_HOSTS=localhost,127.0.0.1,champion-pro-samuel-demonstrates.trycloudflare.com
set PUBLIC_BASE_URL=https://champion-pro-samuel-demonstrates.trycloudflare.com
set TRUST_PROXY_HEADERS=true
start "" /B python -m uvicorn app:app --host 127.0.0.1 --port 8000
