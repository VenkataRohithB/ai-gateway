"""Budget ledger and audit writes. All money is integer micro-USD.

Reserve-before-forward makes the per-agent and per-mandate ceilings hard under concurrency:
a request is forwarded only after ONE transaction has (a) locked the agent-mandate assignment,
(b) atomically added its worst-case cost to the agent's and the mandate's reserved_micros,
re-checking status and remaining budget on the newest row versions. Lock order is always
assignment -> agent -> mandate (settle: agent -> mandate), so these transactions cannot deadlock.
"""
import json
import uuid

AUDIT_COLUMNS = {"kind", "actor", "request_id", "agent_id", "mandate_id", "model", "decision", "reason_code",
                 "policy_version", "reserved_micros", "cost_micros", "cost_source", "prompt_tokens",
                 "completion_tokens", "finish_reason", "upstream_status", "detail"}


async def audit(conn, **fields) -> None:
    assert set(fields) <= AUDIT_COLUMNS, set(fields) - AUDIT_COLUMNS
    fields["detail"] = json.dumps(fields.get("detail") or {}, default=str)
    cols = list(fields)
    values = ", ".join(f"${i + 1}" + ("::jsonb" if c == "detail" else "") for i, c in enumerate(cols))
    await conn.execute(f"INSERT INTO audit_events ({', '.join(cols)}) VALUES ({values})", *fields.values())


class _GuardFailed(Exception):
    pass


async def reserve(pool, request_id: uuid.UUID, agent_id: str, mandate_id: str, model: str, amount: int,
                  policy_version: str) -> str | None:
    """Reserve `amount` on agent and mandate and audit the allow, atomically.

    Returns None on success, else the guard that failed: "assignment", "agent" or "mandate".
    "agent"/"mandate" mean inactive OR over budget; the caller re-reads facts to pick the reason.
    """
    async with pool.acquire() as c:
        try:
            async with c.transaction():
                # FOR SHARE: a concurrent unassign (DELETE) waits for us, or we see it and fail.
                if not await c.fetchval("SELECT 1 FROM agent_mandates WHERE agent_id=$1 AND mandate_id=$2 FOR SHARE",
                                        agent_id, mandate_id):
                    raise _GuardFailed("assignment")
                if not await c.fetchval(
                        "UPDATE agents SET reserved_micros = reserved_micros + $2 WHERE id=$1 AND status='active' "
                        "AND spent_micros + reserved_micros + $2 <= budget_limit_micros RETURNING 1", agent_id, amount):
                    raise _GuardFailed("agent")
                if not await c.fetchval(
                        "UPDATE mandates SET reserved_micros = reserved_micros + $2 WHERE id=$1 AND status='active' "
                        "AND spent_micros + reserved_micros + $2 <= budget_limit_micros RETURNING 1", mandate_id, amount):
                    raise _GuardFailed("mandate")
                await c.execute("INSERT INTO reservations (request_id, agent_id, mandate_id, model, amount_micros) "
                                "VALUES ($1,$2,$3,$4,$5)", request_id, agent_id, mandate_id, model, amount)
                await audit(c, kind="request", actor=agent_id, request_id=request_id, agent_id=agent_id,
                            mandate_id=mandate_id, model=model, decision="allow", policy_version=policy_version,
                            reserved_micros=amount)
        except _GuardFailed as failed:
            return failed.args[0]
    return None


async def settle(pool, request_id: uuid.UUID, outcome: str, cost: int | None, cost_source: str,
                 actor: str, **audit_extra) -> dict | None:
    """Close a held reservation. outcome: "settled" (charge `cost`; None = charge the reserved amount)
    or "released" (charge 0). Touches the ledger only if this call is the one that closed it;
    a late settle after the sweeper is audited as late_ignored. Returns the ledger change or None."""
    if outcome == "released":
        cost, cost_source = 0, "none"
    async with pool.acquire() as c, c.transaction():
        row = await c.fetchrow(
            "UPDATE reservations SET status=$2, cost_micros=coalesce($3, amount_micros), settled_at=now() "
            "WHERE request_id=$1 AND status='held' "
            "RETURNING agent_id, mandate_id, model, amount_micros, cost_micros", request_id, outcome, cost)
        if row:
            for table, id_ in (("agents", row["agent_id"]), ("mandates", row["mandate_id"])):
                await c.execute(f"UPDATE {table} SET reserved_micros = reserved_micros - $2, "
                                f"spent_micros = spent_micros + $3, updated_at = now() WHERE id=$1",
                                id_, row["amount_micros"], row["cost_micros"])
            await audit(c, kind="settle", actor=actor, request_id=request_id, agent_id=row["agent_id"],
                        mandate_id=row["mandate_id"], model=row["model"], decision=outcome,
                        reserved_micros=row["amount_micros"], cost_micros=row["cost_micros"],
                        cost_source=cost_source, **audit_extra)
            return dict(row)
        info = await c.fetchrow("SELECT agent_id, mandate_id, model FROM reservations WHERE request_id=$1", request_id)
        await audit(c, kind="settle", actor=actor, request_id=request_id, decision="late_ignored",
                    cost_micros=cost, cost_source="late_ignored", **(dict(info) if info else {}), **audit_extra)
        return None


async def sweep(pool, older_than_seconds: int | None) -> int:
    """Charge abandoned reservations (gateway crash, lost task) at their reserved amount.
    older_than_seconds=None sweeps every held row (startup; assumes a single gateway replica)."""
    # ponytail: single replica; tag reservations with an instance id before running several gateways.
    ids = await pool.fetch(
        "SELECT request_id FROM reservations WHERE status='held' "
        "AND ($1::int IS NULL OR created_at < now() - make_interval(secs => $1))", older_than_seconds)
    for r in ids:  # one transaction per reservation
        await settle(pool, r["request_id"], "settled", None, "reservation", actor="sweeper", detail={"swept": True})
    return len(ids)
