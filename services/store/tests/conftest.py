import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STORE = Path(__file__).resolve().parents[1]

# Offline, dev-mode, fast store for tests. Must be set before app.main is imported.
os.environ.pop("FLAGSMITH_SERVER_KEY", None)
os.environ.setdefault("INSIDIOUS_DEV", "1")
os.environ.setdefault("HEAVY_WORK_MS", "2")
os.environ.setdefault("PAYMENTS_URL", "http://payments-mock.test:8001")
os.environ.setdefault("GUARDIAN_URL", "http://guardian.test:8002")
os.environ.setdefault("INSIDIOUS_CONFIG", str(ROOT / "insidious.yaml"))
os.environ.setdefault("INSIDIOUS_FLAGS", str(ROOT / "flagsmith" / "flags.yaml"))
# Fresh SQLite file per test session (the SDK's SQLite fallback path; production uses Postgres).
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="fk-store-"), "store.db")
os.environ.setdefault("DB_QUERY_WORK_MS", "1")

sys.path.insert(0, str(STORE))
sys.path.insert(0, str(ROOT / "sdk"))
