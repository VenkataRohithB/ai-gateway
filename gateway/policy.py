"""Load and evaluate config/policy.toml: an ordered list of data-only `forbid` rules.

Kept deliberately small so a Cedar evaluator can replace it behind the same evaluate() call.
"""
import hashlib, tomllib

FACTS = {"agent.status", "assignment.exists", "mandate.status", "model.allowed"}


class Policy:
    def __init__(self, path: str):
        raw = open(path, "rb").read()
        self.version = hashlib.sha256(raw).hexdigest()[:12]
        self.rules = tomllib.loads(raw.decode())["forbid"]
        for i, rule in enumerate(self.rules):
            if not rule.get("reason") or not rule.get("when"):
                raise ValueError(f"policy rule #{i + 1}: needs `reason` and `when`")
            unknown = set(rule["when"]) - FACTS
            if unknown:  # a typo must never silently disable a rule
                raise ValueError(f"policy rule {rule['reason']}: unknown fact(s) {sorted(unknown)}")

    def evaluate(self, facts: dict) -> dict:
        for rule in self.rules:
            if all(facts[k] == v for k, v in rule["when"].items()):
                return {"allow": False, "reason": rule["reason"], "message": rule.get("message", rule["reason"]),
                        "policy_version": self.version}
        return {"allow": True, "reason": None, "message": "allowed", "policy_version": self.version}
