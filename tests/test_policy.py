"""Unit tests for config/policy.toml + gateway/policy.py. No database, no network."""
import itertools, os

import pytest

from policy import FACTS, Policy

POLICY_FILE = os.environ.get("POLICY_FILE", "/config/policy.toml")
OK = {"agent.status": "active", "assignment.exists": True, "mandate.status": "active", "model.allowed": True}


@pytest.fixture(scope="module")
def policy():
    return Policy(POLICY_FILE)


def test_all_facts_ok_is_allowed(policy):
    d = policy.evaluate(OK)
    assert d == {"allow": True, "reason": None, "message": "allowed", "policy_version": policy.version}


@pytest.mark.parametrize("change, reason", [
    ({"agent.status": "revoked"}, "AGENT_REVOKED"),
    ({"agent.status": "suspended"}, "AGENT_INACTIVE"),
    ({"assignment.exists": False}, "MANDATE_NOT_ASSIGNED"),
    ({"assignment.exists": False, "mandate.status": "unknown"}, "MANDATE_NOT_ASSIGNED"),  # no enumeration
    ({"mandate.status": "suspended"}, "MANDATE_INACTIVE"),
    ({"model.allowed": False}, "MODEL_NOT_ALLOWED"),
    ({"agent.status": "revoked", "mandate.status": "suspended", "model.allowed": False}, "AGENT_REVOKED"),  # order
])
def test_each_rule_denies_with_its_reason(policy, change, reason):
    d = policy.evaluate({**OK, **change})
    assert d["allow"] is False and d["reason"] == reason and d["policy_version"] == policy.version and d["message"]


def test_policy_agrees_with_sql_guard_for_every_fact_combination(policy):
    """db.reserve() re-checks agent active + assignment + mandate active in SQL. For every combination
    of facts, the policy must never allow something the SQL guard rejects."""
    for agent, assigned, mandate, model in itertools.product(
            ["active", "suspended", "revoked"], [True, False], ["active", "suspended", "unknown"], [True, False]):
        facts = {"agent.status": agent, "assignment.exists": assigned, "mandate.status": mandate, "model.allowed": model}
        sql_guard_passes = agent == "active" and assigned and mandate == "active"
        if policy.evaluate(facts)["allow"]:
            assert sql_guard_passes and model, facts


def test_unknown_fact_refuses_to_load(tmp_path):
    bad = tmp_path / "p.toml"
    bad.write_text('[[forbid]]\nreason = "X"\nwhen = { "agent.statsu" = "revoked" }\n')
    with pytest.raises(ValueError, match="unknown fact"):
        Policy(str(bad))


def test_rule_without_reason_refuses_to_load(tmp_path):
    bad = tmp_path / "p.toml"
    bad.write_text('[[forbid]]\nwhen = { "agent.status" = "revoked" }\n')
    with pytest.raises(ValueError):
        Policy(str(bad))


def test_version_tracks_file_content(tmp_path, policy):
    f = tmp_path / "p.toml"
    f.write_bytes(open(POLICY_FILE, "rb").read())
    assert Policy(str(f)).version == policy.version
    f.write_bytes(open(POLICY_FILE, "rb").read() + b"\n# changed\n")
    assert Policy(str(f)).version != policy.version


def test_policy_only_uses_known_facts(policy):
    assert {k for r in policy.rules for k in r["when"]} <= FACTS
