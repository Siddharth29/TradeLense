# Verification

Run backend checks from the repository root with the Python dependencies installed.

~~~powershell
python -m pytest tests/test_offline_replay.py -q
python -m pytest tests/test_account_guard.py tests/test_risk_service.py tests/test_trade_monitor.py -q
~~~

For frontend tests and the production build, run npm test and npm run build in apps/frontend.

Real-terminal checks use an explicitly configured demo server, login, and password through TRADELENS_TEST_MT5_SERVER, TRADELENS_TEST_MT5_LOGIN, and TRADELENS_TEST_MT5_PASSWORD.
