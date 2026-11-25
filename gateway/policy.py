"""Access policy, evaluated by AWS Cedar (cedarpy) on facts gathered fresh for each request.

Rules are stored as data: an ordered list of deny rules {reason, when, message}. Each compiles to a Cedar
`forbid` annotated @id("<REASON>"), plus one `permit`, validated against SCHEMA. Two actions: `call` (a model
call; resource Model) and `call_tool` (an MCP message to a tool server; resource ToolServer). A rule on model
facts applies to model calls, one on tool facts to tool calls, any other rule to both.
Cedar is order-independent, so when several forbids match, the reason shown is the first matching rule in
list order (built-ins first). Any Cedar error denies: the evaluator fails closed.

Built-in rules protect the kill switches and cannot be edited (db.reserve() also enforces the same
states in SQL). Custom rules are written in the admin UI and stored as numbered versions in the
policy_versions table, in the same JSON format; they are compiled to Cedar when loaded or saved.
"""
import hashlib, json, logging, re

import cedarpy

STATE_FACTS = {"agent.status", "assignment.exists", "mandate.status", "model.allowed", "server.allowed", "tool.allowed"}
CUSTOM_FACTS = {"agent.id", "mandate.id", "model.name", "tool.server"}  # what a custom rule may match on
FACTS = STATE_FACTS | CUSTOM_FACTS
MODEL_FACTS, TOOL_FACTS = {"model.allowed", "model.name"}, {"server.allowed", "tool.allowed", "tool.server"}

BUILTIN = [
    {"reason": "AGENT_REVOKED", "when": {"agent.status": "revoked"}, "message": "agent credential has been revoked"},
    {"reason": "AGENT_INACTIVE", "when": {"agent.status": "suspended"}, "message": "agent is suspended"},
    # unknown and unassigned mandates get the same answer, so agents cannot discover mandate names
    {"reason": "MANDATE_NOT_ASSIGNED", "when": {"assignment.exists": False}, "message": "agent is not assigned to this mandate"},
    {"reason": "MANDATE_NOT_ASSIGNED", "when": {"mandate.status": "unknown"}, "message": "agent is not assigned to this mandate"},
    {"reason": "MANDATE_INACTIVE", "when": {"mandate.status": "suspended"}, "message": "mandate is suspended"},
    {"reason": "MODEL_NOT_ALLOWED", "when": {"model.allowed": False}, "message": "model is not allowed under this mandate"},
]
TOOL_BUILTIN = [  # tool calls only; unknown servers get the same answer as servers the mandate may not use
    {"reason": "TOOL_SERVER_NOT_ALLOWED", "when": {"server.allowed": False}, "message": "tool server is not allowed under this mandate"},
    {"reason": "MCP_TOOL_NOT_ALLOWED", "when": {"tool.allowed": False}, "message": "tool is not allowed under this mandate"},
]
EVAL_ERROR = {"reason": "POLICY_ERROR", "message": "request could not be checked against the policy"}
REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,47}$")

SCHEMA_TEXT = """entity Agent = { status: String };
entity Model;
entity ToolServer;
action call appliesTo {
  principal: Agent,
  resource: Model,
  context: { mandate: { id: String, status: String }, assignment: { exists: Bool }, model: { allowed: Bool } }
};
action call_tool appliesTo {
  principal: Agent,
  resource: ToolServer,
  context: { mandate: { id: String, status: String }, assignment: { exists: Bool }, server: { allowed: Bool },
             tool: { allowed: Bool } }
};"""
SCHEMA = cedarpy.Schema.from_str(SCHEMA_TEXT)
CONDITION = {  # fact -> Cedar condition; {} is the literal
    "agent.status": "principal.status == {}", "agent.id": "principal == Agent::{}", "model.name": "resource == Model::{}",
    "mandate.id": "context.mandate.id == {}", "mandate.status": "context.mandate.status == {}",
    "assignment.exists": "context.assignment.exists == {}", "model.allowed": "context.model.allowed == {}",
    "tool.server": "resource == ToolServer::{}", "server.allowed": "context.server.allowed == {}",
    "tool.allowed": "context.tool.allowed == {}",
}
log = logging.getLogger("gateway.policy")


def literal(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    esc = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0"}
    return '"' + "".join(esc.get(c) or (c if c.isprintable() else f"\\u{{{ord(c):x}}}") for c in v) + '"'


def action_scope(when: dict) -> str:
    if set(when) & MODEL_FACTS:
        return 'action == Action::"call"'
    if set(when) & TOOL_FACTS:
        return 'action == Action::"call_tool"'
    return 'action in [Action::"call", Action::"call_tool"]'


def to_cedar(rules: list[dict]) -> str:
    """Rule i becomes Cedar policy id "policy<i>"; the permit comes last."""
    forbids = [f'@id("{r["reason"]}")\nforbid (principal, {action_scope(r["when"])}, resource)\nwhen {{ '
               + " && ".join(CONDITION[k].format(literal(v)) for k, v in r["when"].items()) + " };" for r in rules]
    return "\n\n".join(forbids + ['@id("ALLOW")\npermit (principal, action in [Action::"call", Action::"call_tool"], resource);']) + "\n"


def validate_custom(rules) -> list[dict]:
    """Normalise custom rules; raise ValueError with a message an admin can act on."""
    if not isinstance(rules, list) or len(rules) > 200:
        raise ValueError("rules must be a list of at most 200 rules")
    builtin_reasons = {r["reason"] for r in BUILTIN + TOOL_BUILTIN} | {EVAL_ERROR["reason"]}
    out = []
    for i, rule in enumerate(rules, 1):
        if not isinstance(rule, dict) or set(rule) - {"reason", "when", "message"}:
            raise ValueError(f"rule {i}: only reason, when and message are allowed")
        reason, when, message = rule.get("reason"), rule.get("when"), rule.get("message") or ""
        if not isinstance(reason, str) or not REASON.match(reason) or reason in builtin_reasons:
            raise ValueError(f"rule {i}: reason must be an UPPER_SNAKE_CASE code (3-48 chars) not used by a built-in rule")
        if not isinstance(when, dict) or not when:
            raise ValueError(f"rule {i}: needs at least one condition")
        if unknown := set(when) - CUSTOM_FACTS:  # a typo must never silently disable a rule
            raise ValueError(f"rule {i}: unknown condition(s) {sorted(unknown)}; allowed: {sorted(CUSTOM_FACTS)}")
        if set(when) & MODEL_FACTS and set(when) & TOOL_FACTS:  # would never match anything
            raise ValueError(f"rule {i}: a rule matches a model or a tool server, not both")
        if not all(isinstance(v, str) and 0 < len(v) <= 128 for v in when.values()):
            raise ValueError(f"rule {i}: condition values must be non-empty text")
        if not isinstance(message, str) or len(message) > 300:
            raise ValueError(f"rule {i}: message must be text of at most 300 characters")
        out.append({"reason": reason, "when": dict(sorted(when.items())), "message": message or reason})
    return out


class Policy:
    def __init__(self, custom: list | None = None, number: int = 0):
        self.custom = validate_custom([] if custom is None else custom)
        self.rules = BUILTIN + TOOL_BUILTIN + self.custom
        self.number = number
        digest = hashlib.sha256(json.dumps(self.rules, sort_keys=True).encode()).hexdigest()[:8]
        self.version = f"v{number}-{digest}"
        self.cedar = to_cedar(self.rules)
        result = cedarpy.validate_policies(self.cedar, SCHEMA)
        if not result.validation_passed:
            raise ValueError(f"policy does not validate against the Cedar schema: {[e.error for e in result.errors]}")
        self.policy_set = cedarpy.PolicySet.from_str(self.cedar)
        self.priority = {f"policy{i}": i for i in range(len(self.rules))}

    def decide(self, facts: dict) -> dict | None:
        """The first matching rule in list order, or None to allow. Raises on any Cedar error."""
        agent = {"type": "Agent", "id": facts["agent.id"]}
        context = {"mandate": {"id": facts["mandate.id"], "status": facts["mandate.status"]},
                   "assignment": {"exists": facts["assignment.exists"]}}
        if "tool.server" in facts:  # an MCP message to a tool server
            action, resource = "call_tool", {"type": "ToolServer", "id": facts["tool.server"]}
            context |= {"server": {"allowed": facts["server.allowed"]}, "tool": {"allowed": facts["tool.allowed"]}}
        else:
            action, resource = "call", {"type": "Model", "id": facts["model.name"]}
            context |= {"model": {"allowed": facts["model.allowed"]}}
        request = {"principal": agent, "action": {"type": "Action", "id": action}, "resource": resource, "context": context}
        entities = [{"uid": agent, "attrs": {"status": facts["agent.status"]}, "parents": []},
                    {"uid": resource, "attrs": {}, "parents": []}]
        result = cedarpy.is_authorized(request, self.policy_set, entities, SCHEMA)
        if result.diagnostics.errors:
            raise RuntimeError(f"cedar errors: {result.diagnostics.errors}")
        if result.allowed:
            return None
        matched = [self.priority[p] for p in result.diagnostics.reasons if p in self.priority]
        if not matched:
            raise RuntimeError(f"cedar denied without a matching rule: {result.decision}")
        return self.rules[min(matched)]

    def evaluate(self, facts: dict) -> dict:
        try:
            rule = self.decide(facts)
        except Exception as e:  # fail closed: deny, never allow
            log.error("policy evaluation failed: %s", e)
            rule = EVAL_ERROR
        if rule:
            return {"allow": False, "reason": rule["reason"], "message": rule["message"], "policy_version": self.version}
        return {"allow": True, "reason": None, "message": "allowed", "policy_version": self.version}
