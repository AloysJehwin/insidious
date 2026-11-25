import os
from pathlib import Path

import pytest

from insidious_cli.settings import flags_spec

REPO = Path(__file__).resolve().parents[2]

ENV_TO_CLEAR = [
    "FLAGSMITH_ORG_API_KEY", "FLAGSMITH_ENVIRONMENT_KEY", "FLAGSMITH_PROJECT_ID", "FLAGSMITH_ADMIN_URL",
    "NUDGEBEE_URL", "NUDGEBEE_API_TOKEN", "NUDGEBEE_ACCOUNT_ID", "NUDGEBEE_WF_PRESALE_ID", "NUDGEBEE_DRY_RUN",
    "STORE_URL", "GUARDIAN_URL", "OPS_BOT_URL", "INSIDIOUS_CONFIG", "INSIDIOUS_FLAGS",
    "FESTIVEKIT_CONFIG", "FESTIVEKIT_FLAGS", "FESTIVEKIT_ROOT", "FESTIVEKIT_NO_DOTENV", "FESTIVEKIT_REPORTS_DIR",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in ENV_TO_CLEAR:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INSIDIOUS_ROOT", str(REPO))
    monkeypatch.setenv("INSIDIOUS_NO_DOTENV", "1")
    monkeypatch.setenv("INSIDIOUS_REPORTS_DIR", str(tmp_path / "reports"))
    yield


@pytest.fixture
def spec():
    return flags_spec(REPO)


def set_env(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, v)
    return os.environ
