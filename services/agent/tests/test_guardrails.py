import pytest

from app.catalog import GuardrailError, parse_cpu, parse_memory, validate_action
from app.config import DEFAULT_GUARDRAILS, Settings
from app.executors import build_read_url
from app.flags import build_runtime

G = dict(DEFAULT_GUARDRAILS)


def act(t, **params):
    return {"type": t, "params": params}


def test_scale_within_limits_is_normalised():
    a = validate_action(act("k8s.scale", namespace="store", deployment="store-api", replicas="3"), G)
    assert a.params == {"namespace": "store", "deployment": "store-api", "replicas": 3}


@pytest.mark.parametrize("replicas", [6, 50, 0, -1, 2.5])
def test_scale_replicas_out_of_range_rejected(replicas):
    with pytest.raises(GuardrailError) as e:
        validate_action(act("k8s.scale", namespace="store", deployment="store-api", replicas=replicas), G)
    assert "replicas" in str(e.value)


def test_scale_wrong_namespace_rejected():
    with pytest.raises(GuardrailError, match="namespace 'kube-system' is not allowed"):
        validate_action(act("k8s.scale", namespace="kube-system", deployment="coredns", replicas=2), G)


def test_set_resources_wrong_namespace_rejected():
    with pytest.raises(GuardrailError, match="namespace"):
        validate_action(act("k8s.set_resources", namespace="insidious", deployment="guardian", container="guardian",
                            cpu_limit="1", memory_limit="512Mi"), G)


@pytest.mark.parametrize("cls", ["db.r5.4xlarge", "db.t4g.large", "db.m6g.xlarge"])
def test_disallowed_instance_class_rejected(cls):
    with pytest.raises(GuardrailError, match="instance class"):
        validate_action(act("aws.rds.modify_instance_class", db_id="insidious-db", instance_class=cls), G)
    with pytest.raises(GuardrailError, match="instance class"):
        validate_action(act("aws.rds.create_read_replica", source_db_id="insidious-db",
                            replica_id="insidious-db-read-1", instance_class=cls), G)


def test_rds_id_prefix_enforced():
    # pre-rename RDS identifiers (festivekit-primary, ...) stay manageable
    ok = validate_action(act("aws.rds.modify_instance_class", db_id="festivekit-primary", instance_class="db.t4g.small"), G)
    assert ok.params["db_id"] == "festivekit-primary"
    with pytest.raises(GuardrailError, match="must start with one of \\['insidious-', 'festivekit-'\\]"):
        validate_action(act("aws.rds.modify_instance_class", db_id="prod-orders", instance_class="db.t4g.small"), G)


def test_resources_above_max_rejected():
    with pytest.raises(GuardrailError, match="max_memory_limit"):
        validate_action(act("k8s.set_resources", namespace="store", deployment="store-api", container="store-api",
                            cpu_limit="1", memory_limit="4Gi"), G)
    with pytest.raises(GuardrailError, match="max_cpu_limit"):
        validate_action(act("k8s.set_resources", namespace="store", deployment="store-api", container="store-api",
                            cpu_limit="2500m", memory_limit="1Gi"), G)
    ok = validate_action(act("k8s.set_resources", namespace="store", deployment="store-api", container="store-api",
                             cpu_limit="1000m", memory_limit="1024Mi"), G)
    assert ok.params["cpu_limit"] == "1" and ok.params["memory_limit"] == "1Gi"


def test_unknown_action_and_protected_flag_rejected():
    with pytest.raises(GuardrailError, match="not in the action catalog"):
        validate_action(act("shell.exec", cmd="rm -rf /"), G)
    with pytest.raises(GuardrailError, match="protected"):
        validate_action(act("flagsmith.set_flag", flag="ai_agent", enabled=False), G)


def test_guardrails_from_insidious_yaml_override_defaults():
    s = Settings.from_env(cfg={"agent": {"guardrails": {"max_replicas": 2, "allowed_namespaces": ["shop"]}}},
                          flags_doc={})
    assert s.guardrails["max_replicas"] == 2 and s.guardrails["rds_id_prefix"] == ["insidious-", "festivekit-"]
    with pytest.raises(GuardrailError):
        validate_action(act("k8s.scale", namespace="shop", deployment="store-api", replicas=3), s.guardrails)


def test_quantities():
    assert parse_cpu("500m") == 0.5 and parse_cpu("2") == 2.0
    assert parse_memory("512Mi") == 512 * 2**20 and parse_memory("1G") == 10**9


def test_build_read_url_keeps_encoded_credentials():
    url = build_read_url("postgresql+psycopg://jeffy:p%40ss%2Fw@db.local:5432/jeffy?sslmode=require",
                         "insidious-db-read-1.abc.rds.amazonaws.com", 5432)
    assert url == ("postgresql+psycopg://jeffy:p%40ss%2Fw@insidious-db-read-1.abc.rds.amazonaws.com:5432/jeffy"
                   "?sslmode=require")
    assert build_read_url("", "x", 5432) is None


def test_runtime_from_flagsmith_json():
    s = Settings()
    rt = build_runtime(s, True, '{"provider":"bedrock","model":"claude-opus-5-5","autopilot":false,'
                                '"max_actions":3,"language_hint":"plain"}', "flagsmith")
    assert (rt.provider, rt.model, rt.max_actions, rt.autopilot) == ("bedrock", "claude-opus-5-5", 3, False)
    rt2 = build_runtime(s, False, "not json", "flagsmith")
    assert rt2.ai_agent is False and rt2.model == "claude-sonnet-5-5" and rt2.provider == "anthropic"


class _FakeFlags:
    def __init__(self, enabled, cfg):
        self.enabled, self.cfg = enabled, cfg

    def is_feature_enabled(self, name):
        assert name == "ai_agent"
        return self.enabled

    def get_feature_value(self, name):
        assert name == "agent_config"
        return self.cfg


class _FakeFlagsmith:
    def __init__(self, **kw):
        self.kw = kw

    def get_environment_flags(self):
        return _FakeFlags(False, '{"model": "claude-opus-5-5", "autopilot": true}')


async def test_runtime_config_reads_flagsmith_sdk_with_local_evaluation():
    from app.flags import RuntimeConfig

    made = {}

    def factory(**kw):
        made.update(kw)
        return _FakeFlagsmith(**kw)

    rc = RuntimeConfig(Settings(flagsmith_server_key="ser.abc"), client_factory=factory)
    rt = await rc.get()
    assert made["enable_local_evaluation"] is True and made["environment_key"] == "ser.abc"
    assert (rt.ai_agent, rt.model, rt.autopilot, rt.source) == (False, "claude-opus-5-5", True, "flagsmith")


async def test_runtime_config_offline_defaults():
    from app.flags import RuntimeConfig

    s = Settings(model="claude-sonnet-5-5",
                 flags_doc={"features": [{"name": "ai_agent", "type": "FLAG", "default_enabled": False}]})
    rt = await RuntimeConfig(s).get()
    assert rt.source == "offline" and rt.ai_agent is False and rt.model == "claude-sonnet-5-5"
