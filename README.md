# IMPULSE MAX 5K - Kraken Pulse Hunter

Local PAPER/REPLAY app. Kraken public data, dynamic USD/EUR spot universe, pulse/contradiction/confidence/cost filters, risk sizing and untouched holdout. LIVE orders remain disabled until validation.

Windows update/start: stop current server with Ctrl+C, then run: cd C:\\Burza ; git pull ; .\\.venv\\Scripts\\Activate.ps1 ; uvicorn app:app --host 127.0.0.1 --port 8765

Open http://127.0.0.1:8765 . Scanner endpoint: http://127.0.0.1:8765/api/pulses . API keys are not needed for this PAPER scanner and must never be committed.
