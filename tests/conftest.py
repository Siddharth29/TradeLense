import json
import os
import sys
from pathlib import Path
import pytest

os.environ.setdefault("TRADELENS_OFFLINE", "false")

# Ensure repo root and packages directory are on sys.path for imports
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PKG_ROOT = ROOT / "packages"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

@pytest.fixture(scope="session", autouse=True)
def isolate_audit_logs(tmp_path_factory):
    """Keep API test startup and simulated trades out of the live audit inputs."""
    from src.common import logger

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(logger, "_LOGS_DIR", tmp_path_factory.mktemp("audit-logs"))
        yield


@pytest.fixture(autouse=True)
def patch_sniper(monkeypatch):
    """Replace the real API call with deterministic JSON."""
    dummy_json = json.dumps(
        {
            "direction": "long",
            "condition": "retest",
            "level": 100.0,
            "stop": 99.0,
            "target": 101.5,
            "confidence": "medium",
        }
    )
    from core import sniper_agent

    async def fake_call_sniper(c15, c5, ts, **kwargs):
        return json.loads(dummy_json)

    monkeypatch.setattr(sniper_agent, "call_sniper", fake_call_sniper)
