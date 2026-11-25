"""Unit tests for gateway/policy.py. No database, no network."""
import itertools, re

import cedarpy
import pytest

import policy as policy_module
from policy import BUILTIN, FACTS, TOOL_BUILTIN, Policy

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


# --------------------------------------------------------------------------- Cedar evaluator
@pytest.mark.parametrize("rule", BUILTIN, ids=lambda r: f"{r['reason']}-{next(iter(r['when']))}")
def test_each_builtin_rule_is_a_cedar_forbid_that_denies(policy, rule):
    assert f'@id("{rule["reason"]}")\nforbid' in policy.cedar
    d = policy.evaluate({**OK, **rule["when"]})
    assert d == {"allow": False, "reason": rule["reason"], "message": rule["message"], "policy_version": policy.version}


def test_generated_cedar_validates_against_the_schema(policy):
    assert cedarpy.validate_policies(policy.cedar, policy_module.SCHEMA).validation_passed
    assert policy.cedar.count("forbid") == len(BUILTIN + TOOL_BUILTIN) and policy.cedar.count("permit") == 1
    assert re.fullmatch(r"v0-[0-9a-f]{8}", policy.version)


def test_custom_rule_compiles_to_cedar_and_denies():
    odd = 'agent "x"\\y\n\x01é'  # quotes, backslash, control chars and non-ASCII must not break out of the literal
    p = Policy([{"reason": "NO_M2_FOR_A", "when": {"model.name": "mock-model-2", "agent.id": "agent-a", "mandate.id": "m1"},
                 "message": "no"}, {"reason": "ODD_AGENT", "when": {"agent.id": odd}}], 7)
    assert ('@id("NO_M2_FOR_A")\nforbid (principal, action == Action::"call", resource)\n'
            'when { principal == Agent::"agent-a" && context.mandate.id == "m1" && resource == Model::"mock-model-2" };') in p.cedar
    assert p.evaluate({**OK, "model.name": "mock-model-2"}) == {"allow": False, "reason": "NO_M2_FOR_A", "message": "no",
                                                                  "policy_version": p.version}
    assert p.evaluate({**OK, "model.name": "mock-model-2", "mandate.id": "m2"})["allow"]
    assert p.evaluate({**OK, "agent.id": odd})["reason"] == "ODD_AGENT"
    assert p.evaluate({**OK, "agent.id": 'agent "x"'})["allow"]
    assert p.custom == Policy(p.custom, 7).custom and p.version == Policy(p.custom, 7).version  # stored JSON round-trips


def test_when_several_rules_match_the_first_in_list_order_wins():
    p = Policy([{"reason": "FIRST_RULE", "when": {"model.name": "mock-model"}},
                {"reason": "SECOND_RULE", "when": {"agent.id": "agent-a", "model.name": "mock-model"}}])
    assert p.evaluate(OK)["reason"] == "FIRST_RULE"
    assert Policy(list(reversed(p.custom))).evaluate(OK)["reason"] == "SECOND_RULE"
    everything = {**OK, "agent.status": "suspended", "assignment.exists": False, "mandate.status": "suspended", "model.allowed": False}
    assert p.evaluate(everything)["reason"] == "AGENT_INACTIVE"
    assert p.evaluate({**everything, "agent.status": "active"})["reason"] == "MANDATE_NOT_ASSIGNED"
    assert p.evaluate({**OK, "mandate.status": "suspended", "model.allowed": False})["reason"] == "MANDATE_INACTIVE"
    assert p.evaluate({**OK, "model.allowed": False})["reason"] == "MODEL_NOT_ALLOWED"


@pytest.mark.parametrize("facts", [
    {**OK, "assignment.exists": "yes"},       # wrong type for the schema
    {**OK, "assignment.exists": None},
    {**OK, "agent.status": 1},
    {k: v for k, v in OK.items() if k != "model.allowed"},  # missing fact
])
def test_bad_facts_fail_closed(policy, facts):
    d = policy.evaluate(facts)
    assert d["allow"] is False and d["reason"] == "POLICY_ERROR" and d["policy_version"] == policy.version


def test_evaluator_error_fails_closed(policy, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cedar exploded")
    monkeypatch.setattr(cedarpy, "is_authorized", boom)
    assert policy.evaluate(OK) == {"allow": False, "reason": "POLICY_ERROR", "message": policy_module.EVAL_ERROR["message"],
                                   "policy_version": policy.version}


@pytest.mark.parametrize("allowed, reasons, errors", [
    (True, ["policy6"], ["a forbid failed to evaluate"]),  # Cedar skips an erroring forbid; we must not allow
    (False, [], []),                                        # deny with no matching rule
])
def test_cedar_errors_and_unexplained_denies_fail_closed(policy, monkeypatch, allowed, reasons, errors):
    diagnostics = type("Diagnostics", (), {"reasons": reasons, "errors": errors})()
    result = type("AuthzResult", (), {"allowed": allowed, "decision": "?", "diagnostics": diagnostics})()
    monkeypatch.setattr(cedarpy, "is_authorized", lambda *a, **k: result)
    assert policy.evaluate(OK)["reason"] == "POLICY_ERROR"


def test_policy_error_reason_is_reserved():
    with pytest.raises(ValueError, match="built-in"):
        Policy([{"reason": "POLICY_ERROR", "when": {"agent.id": "a"}}])


@pytest.mark.parametrize("status, reason", [("revoked", "AGENT_REVOKED"), ("suspended", "AGENT_INACTIVE")])
def test_stopped_agent_is_denied_for_every_combination_of_other_facts(status, reason):
    policies = [Policy(), Policy([{"reason": "BLOCK_M1", "when": {"mandate.id": "m1"}},
                                  {"reason": "BLOCK_A_M2", "when": {"agent.id": "agent-a", "model.name": "mock-model-2"}}], 4)]
    for p, assigned, mandate, allowed, agent, mandate_id, model in itertools.product(
            policies, [True, False], ["active", "suspended", "unknown"], [True, False],
            ["agent-a", "agent-b", 'q"uote'], ["m1", "m2", ""], ["mock-model", "mock-model-2", "x"]):
        facts = {"agent.status": status, "assignment.exists": assigned, "mandate.status": mandate, "model.allowed": allowed,
                 "agent.id": agent, "mandate.id": mandate_id, "model.name": model}
        d = p.evaluate(facts)
        assert d["allow"] is False and d["reason"] == reason, facts


# --------------------------------------------------------------------------- tool calls (call_tool action)
TOOL = {"agent.status": "active", "assignment.exists": True, "mandate.status": "active", "server.allowed": True,
        "tool.allowed": True, "agent.id": "agent-a", "mandate.id": "m1", "tool.server": "fetch"}


@pytest.mark.parametrize("change, reason", [
    ({"agent.status": "suspended"}, "AGENT_INACTIVE"), ({"agent.status": "revoked"}, "AGENT_REVOKED"),
    ({"assignment.exists": False}, "MANDATE_NOT_ASSIGNED"), ({"mandate.status": "suspended"}, "MANDATE_INACTIVE"),
    ({"server.allowed": False}, "TOOL_SERVER_NOT_ALLOWED"), ({"tool.allowed": False}, "MCP_TOOL_NOT_ALLOWED"),
])
def test_tool_calls_meet_the_same_kill_switches(policy, change, reason):
    assert policy.evaluate(TOOL)["allow"]
    assert policy.evaluate({**TOOL, **change})["reason"] == reason


def test_rules_apply_to_the_action_their_facts_belong_to():
    p = Policy([{"reason": "NO_M2_MODEL", "when": {"model.name": "mock-model-2"}},
                {"reason": "NO_FETCH_FOR_A", "when": {"agent.id": "agent-a", "tool.server": "fetch"}},
                {"reason": "BLOCK_B", "when": {"agent.id": "agent-b"}}])
    assert '@id("NO_FETCH_FOR_A")\nforbid (principal, action == Action::"call_tool", resource)' in p.cedar
    assert '@id("BLOCK_B")\nforbid (principal, action in [Action::"call", Action::"call_tool"], resource)' in p.cedar
    assert p.evaluate({**TOOL, "tool.server": "fetch"})["reason"] == "NO_FETCH_FOR_A"
    assert p.evaluate({**TOOL, "tool.server": "time"})["allow"]
    assert p.evaluate({**OK, "model.name": "mock-model-2"})["reason"] == "NO_M2_MODEL"
    assert p.evaluate(OK)["allow"]                                   # a tool-server rule never touches model calls
    assert p.evaluate({**TOOL, "agent.id": "agent-b"})["reason"] == p.evaluate({**OK, "agent.id": "agent-b"})["reason"] == "BLOCK_B"
    with pytest.raises(ValueError, match="not both"):
        Policy([{"reason": "NEVER", "when": {"model.name": "x", "tool.server": "y"}}])


@pytest.mark.parametrize("status, reason", [("revoked", "AGENT_REVOKED"), ("suspended", "AGENT_INACTIVE")])
def test_stopped_agent_is_denied_every_tool_call(status, reason):
    p = Policy([{"reason": "BLOCK_FETCH", "when": {"tool.server": "fetch"}}], 2)
    for assigned, mandate, server_ok, tool_ok, server in itertools.product(
            [True, False], ["active", "suspended", "unknown"], [True, False], [True, False], ["fetch", "time", 'q"x']):
        facts = {**TOOL, "agent.status": status, "assignment.exists": assigned, "mandate.status": mandate,
                 "server.allowed": server_ok, "tool.allowed": tool_ok, "tool.server": server}
        assert p.evaluate(facts) == {"allow": False, "reason": reason, "message": p.evaluate(facts)["message"],
                                     "policy_version": p.version}, facts


def test_tool_facts_missing_fail_closed(policy):
    assert policy.evaluate({k: v for k, v in TOOL.items() if k != "tool.allowed"})["reason"] == "POLICY_ERROR"
