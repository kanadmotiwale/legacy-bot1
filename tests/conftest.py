from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx
import pytest

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parent.parent

# Fake credentials for the synthetic app; the test harness plays the role of the secret store.
os.environ.setdefault("MOCKBANK_USER", "teller01")
os.environ.setdefault("MOCKBANK_PASSWORD", "Plant3d-S3cret!")


@pytest.fixture(scope="session")
def app_server():
    import uvicorn

    from target_app.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            if httpx.get(f"{BASE}/login").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    httpx.post(f"{BASE}/__admin/reset")
    yield BASE
    server.should_exit = True


@pytest.fixture(scope="session")
def policy():
    from cua.policy.gate import PolicyConfig

    cfg = PolicyConfig.load(ROOT / "config/policy.yaml")
    cfg.allowed_origins.append(BASE)
    return cfg


@pytest.fixture(scope="session")
def profile():
    from cua.artifact.profile import AppProfile

    return AppProfile.load(ROOT / "config/apps/mockcore.yaml")


def inject(preset: str | None) -> None:
    httpx.post(f"{BASE}/__admin/conditions", json={"preset": preset} if preset else {})
