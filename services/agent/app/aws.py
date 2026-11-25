"""boto3 RDS client factory (imported lazily so tests and mock mode never touch AWS)."""
from __future__ import annotations

from typing import Any, Callable

RdsFactory = Callable[[], Any]


def default_rds_factory(region: str) -> RdsFactory:
    cache: dict[str, Any] = {}

    def make() -> Any:
        if "c" not in cache:
            import boto3
            from botocore.config import Config

            cache["c"] = boto3.client("rds", region_name=region,
                                      config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}))
        return cache["c"]

    return make


def describe_instance(rds: Any, db_id: str) -> dict:
    resp = rds.describe_db_instances(DBInstanceIdentifier=db_id)
    inst = (resp.get("DBInstances") or [{}])[0]
    ep = inst.get("Endpoint") or {}
    return {
        "id": inst.get("DBInstanceIdentifier", db_id),
        "status": inst.get("DBInstanceStatus"),
        "class": inst.get("DBInstanceClass"),
        "engine": inst.get("Engine"),
        "endpoint": ep.get("Address"),
        "port": ep.get("Port"),
        "multi_az": inst.get("MultiAZ"),
        "replicas": list(inst.get("ReadReplicaDBInstanceIdentifiers") or []),
        "replica_of": inst.get("ReadReplicaSourceDBInstanceIdentifier"),
    }
