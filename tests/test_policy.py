"""Unit tests for gateway/policy.py. No database, no network."""
import itertools

import pytest

from policy import BUILTIN, FACTS, Policy

OK = {"agent.status": "active", "assignment.exists": True, "mandate.status": "active", "model.allowed": True,
      "agent.id": "agent-a", "mandate.id": "m1", "model.name": "mock-model"}


@pytest.fixture(scope="module")
def policy():
    return Policy()


def test_all_facts_ok_is_allowed(policy):
    d = policy.evaluate(OK)
    assert d == {"allow": True, "reason": None, "message": "allowed", "policy_version": policy.version}


@pytest.mark.parametrize("change, reason", [
    ({"agent.status": "revoked"}, "AGENT_REVOKED"),
    ({"agent.status": "suspended"}, "AGENT_INACTIVE"),
    ({"assignment.exists": False}, "MANDATE_NOT_ASSIGNED"),
    ({"assignment.exists": False, "mandate.status": "unknown"}, "MANDATE_NOT_ASSIGNED"),  # no enumeration
    ({"mandate.status": "unknown"}, "MANDATE_NOT_ASSIGNED"),
    ({"mandate.status": "suspended"}, "MANDATE_INACTIVE"),
    ({"model.allowed": False}, "MODEL_NOT_ALLOWED"),
    ({"agent.status": "revoked", "mandate.status": "suspended", "model.allowed": False}, "AGENT_REVOKED"),  # order
])
def test_each_builtin_rule_denies_with_its_reason(policy, change, reason):
    d = policy.evaluate({**OK, **change})
    assert d["allow"] is False and d["reason"] == reason and d["policy_version"] == policy.version and d["message"]


def test_policy_agrees_with_sql_guard_for_every_fact_combination(policy):
    """db.reserve() re-checks agent active + assignment + mandate active in SQL. For every combination
    of facts, the policy must never allow something the SQL guard rejects."""
    for agent, assigned, mandate, model in itertools.product(
            ["active", "suspended", "revoked"], [True, False], ["active", "suspended", "unknown"], [True, False]):
        facts = {**OK, "agent.status": agent, "assignment.exists": assigned, "mandate.status": mandate, "model.allowed": model}
        sql_guard_passes = agent == "active" and assigned and mandate == "active"
        if policy.evaluate(facts)["allow"]:
            assert sql_guard_passes and model, facts


def test_custom_rule_denies_only_its_match():
    p = Policy([{"reason": "NO_EXPENSIVE_FOR_A", "when": {"agent.id": "agent-a", "model.name": "mock-model-2"},
                 "message": "agent-a may not use mock-model-2"}], 3)
    assert p.evaluate({**OK, "model.name": "mock-model-2"})["reason"] == "NO_EXPENSIVE_FOR_A"
    assert p.evaluate({**OK, "model.name": "mock-model"})["allow"]
    assert p.evaluate({**OK, "agent.id": "agent-b", "model.name": "mock-model-2"})["allow"]
    assert p.version.startswith("v3-")


def test_builtin_rules_run_before_custom_rules():
    p = Policy([{"reason": "BLOCK_M1", "when": {"mandate.id": "m1"}}])
    assert p.evaluate({**OK, "agent.status": "revoked"})["reason"] == "AGENT_REVOKED"
    assert p.evaluate(OK)["reason"] == "BLOCK_M1"


@pytest.mark.parametrize("rules, error", [
    ([{"reason": "X1X", "when": {"agent.statsu": "a"}}], "unknown condition"),         # typo never disables a rule
    ([{"reason": "X1X", "when": {"agent.status": "active"}}], "unknown condition"),    # built-in facts not editable
    ([{"reason": "X1X", "when": {}}], "at least one condition"),
    ([{"when": {"agent.id": "a"}}], "UPPER_SNAKE_CASE"),
    ([{"reason": "lower", "when": {"agent.id": "a"}}], "UPPER_SNAKE_CASE"),
    ([{"reason": "AGENT_REVOKED", "when": {"agent.id": "a"}}], "built-in"),
    ([{"reason": "X1X", "when": {"agent.id": ""}}], "non-empty"),
    ([{"reason": "X1X", "when": {"agent.id": "a"}, "extra": 1}], "only reason"),
    ({"reason": "X1X"}, "list"),
])
def test_invalid_custom_rules_are_rejected(rules, error):
    with pytest.raises(ValueError, match=error):
        Policy(rules)


def test_version_tracks_rule_content():
    a = Policy([{"reason": "R_ONE", "when": {"agent.id": "x"}}], 1)
    b = Policy([{"reason": "R_ONE", "when": {"agent.id": "y"}}], 1)
    assert a.version != b.version and a.version == Policy([{"reason": "R_ONE", "when": {"agent.id": "x"}}], 1).version


def test_builtin_rules_only_use_known_facts():
    assert {k for r in BUILTIN for k in r["when"]} <= FACTS
