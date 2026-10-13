"""Access policy: an ordered list of data-only deny rules, evaluated on facts gathered fresh for
each request. The first rule whose `when` facts all match denies; no match allows.

Built-in rules protect the kill switches and cannot be edited (db.reserve() also enforces the same
states in SQL). Custom rules are written in the admin UI and stored as numbered versions in the
policy_versions table. Kept small so a Cedar evaluator could replace it behind the same evaluate().
"""
import hashlib, json, re

STATE_FACTS = {"agent.status", "assignment.exists", "mandate.status", "model.allowed"}
CUSTOM_FACTS = {"agent.id", "mandate.id", "model.name"}  # what a custom rule may match on
FACTS = STATE_FACTS | CUSTOM_FACTS

BUILTIN = [
    {"reason": "AGENT_REVOKED", "when": {"agent.status": "revoked"}, "message": "agent credential has been revoked"},
    {"reason": "AGENT_INACTIVE", "when": {"agent.status": "suspended"}, "message": "agent is suspended"},
    # unknown and unassigned mandates get the same answer, so agents cannot discover mandate names
    {"reason": "MANDATE_NOT_ASSIGNED", "when": {"assignment.exists": False}, "message": "agent is not assigned to this mandate"},
    {"reason": "MANDATE_NOT_ASSIGNED", "when": {"mandate.status": "unknown"}, "message": "agent is not assigned to this mandate"},
    {"reason": "MANDATE_INACTIVE", "when": {"mandate.status": "suspended"}, "message": "mandate is suspended"},
    {"reason": "MODEL_NOT_ALLOWED", "when": {"model.allowed": False}, "message": "model is not allowed under this mandate"},
]
REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,47}$")


def validate_custom(rules) -> list[dict]:
    """Normalise custom rules; raise ValueError with a message an admin can act on."""
    if not isinstance(rules, list) or len(rules) > 200:
        raise ValueError("rules must be a list of at most 200 rules")
    builtin_reasons = {r["reason"] for r in BUILTIN}
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
        if not all(isinstance(v, str) and 0 < len(v) <= 128 for v in when.values()):
            raise ValueError(f"rule {i}: condition values must be non-empty text")
        if not isinstance(message, str) or len(message) > 300:
            raise ValueError(f"rule {i}: message must be text of at most 300 characters")
        out.append({"reason": reason, "when": dict(sorted(when.items())), "message": message or reason})
    return out


class Policy:
    def __init__(self, custom: list | None = None, number: int = 0):
        self.custom = validate_custom([] if custom is None else custom)
        self.rules = BUILTIN + self.custom
        self.number = number
        digest = hashlib.sha256(json.dumps(self.rules, sort_keys=True).encode()).hexdigest()[:8]
        self.version = f"v{number}-{digest}"

    def evaluate(self, facts: dict) -> dict:
        for rule in self.rules:
            if all(facts[k] == v for k, v in rule["when"].items()):
                return {"allow": False, "reason": rule["reason"], "message": rule["message"], "policy_version": self.version}
        return {"allow": True, "reason": None, "message": "allowed", "policy_version": self.version}
