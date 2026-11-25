"""Budget ledger, usage records and audit writes. Spend is integer micro-USD; budget limits are
micro-units of the row's own currency (USD or INR, converted with the admin-set rate).

Reserve-before-forward makes the per-agent and per-mandate ceilings hard under concurrency:
a model call is forwarded only after ONE transaction has (a) locked the agent-mandate assignment,
(b) locked the agent and checked its rate limit, (c) atomically added the call's worst-case cost to
the agent's and the mandate's reserved_micros, re-checking status and remaining budget on the newest
row versions. Lock order is always assignment -> agent -> mandate (settle: agent -> mandate ->
usage_monthly), so these transactions cannot deadlock.

Monthly budgets: each ledger row carries `period`, the first day of the current month in India time
(SQL budget_period()). Any write in a new month first rolls spent_micros back to 0. Reserved money is
never rolled, so a call in flight at midnight on the 1st settles normally (into the new month).
"""
import json
import uuid
from decimal import Decimal

AUDIT_COLUMNS = {"kind", "actor", "request_id", "agent_id", "mandate_id", "model", "decision", "reason_code",
                 "policy_version", "reserved_micros", "cost_micros", "cost_source", "prompt_tokens",
                 "completion_tokens", "finish_reason", "upstream_status", "detail"}
USAGE_COLUMNS = {"request_id", "agent_id", "mandate_id", "model_requested", "model_served", "outcome", "reason_code",
                 "http_status", "attempts", "prompt_tokens", "completion_tokens", "cost_micros", "reserved_micros", "cost_source",
                 "latency_ms", "policy_version", "tool_server", "tool_name"}

MONTH_SPENT = "CASE WHEN period = budget_period() THEN spent_micros ELSE 0 END"
LIMIT_USD = ("CASE WHEN budget_currency = 'INR' THEN floor(budget_limit_micros / $3::numeric)::bigint "
             "ELSE budget_limit_micros END")


async def audit(conn, **fields) -> None:
    """Admin changes (append-only trail of who changed what)."""
    assert set(fields) <= AUDIT_COLUMNS, set(fields) - AUDIT_COLUMNS
    fields["detail"] = json.dumps(fields.get("detail") or {}, default=str)
    cols = list(fields)
    values = ", ".join(f"${i + 1}" + ("::jsonb" if c == "detail" else "") for i, c in enumerate(cols))
    await conn.execute(f"INSERT INTO audit_events ({', '.join(cols)}) VALUES ({values})", *fields.values())


async def record_usage(conn, **fields) -> None:
    """One lean row per agent request. Never prompt or response content."""
    assert set(fields) <= USAGE_COLUMNS, set(fields) - USAGE_COLUMNS
    cols = list(fields)
    await conn.execute(f"INSERT INTO usage ({', '.join(cols)}) VALUES ({', '.join(f'${i + 1}' for i in range(len(cols)))})",
                       *fields.values())


class _GuardFailed(Exception):
    pass


async def reserve(pool, reservation_id: uuid.UUID, request_id: uuid.UUID, agent_id: str, mandate_id: str,
                  model: str, amount: int, inr_rate: Decimal) -> str | None:
    """Hold `amount` micro-USD on agent and mandate, atomically.

    Returns None on success, else the guard that failed: "assignment", "rate", "agent" or "mandate".
    "agent"/"mandate" mean inactive OR over budget; the caller re-reads facts to pick the reason.
    """
    async with pool.acquire() as c:
        try:
            async with c.transaction():
                # FOR SHARE: a concurrent unassign (DELETE) waits for us, or we see it and fail.
                if not await c.fetchval("SELECT 1 FROM agent_mandates WHERE agent_id=$1 AND mandate_id=$2 FOR SHARE",
                                        agent_id, mandate_id):
                    raise _GuardFailed("assignment")
                # Locking the agent serialises its requests, so the per-minute count below is exact.
                rpm = await c.fetchval("SELECT rpm_limit FROM agents WHERE id=$1 FOR UPDATE", agent_id)
                if rpm is not None and await c.fetchval(
                        "SELECT count(*) FROM reservations WHERE agent_id=$1 "
                        "AND created_at > clock_timestamp() - interval '60 seconds'", agent_id) >= rpm:
                    raise _GuardFailed("rate")
                for table, id_, guard in (("agents", agent_id, "agent"), ("mandates", mandate_id, "mandate")):
                    if not await c.fetchval(
                            f"UPDATE {table} SET spent_micros = {MONTH_SPENT}, period = budget_period(), "
                            f"reserved_micros = reserved_micros + $2 WHERE id=$1 AND status='active' "
                            f"AND {MONTH_SPENT} + reserved_micros + $2 <= {LIMIT_USD} RETURNING 1", id_, amount, inr_rate):
                        raise _GuardFailed(guard)
                await c.execute("INSERT INTO reservations (request_id, client_request_id, agent_id, mandate_id, model, "
                                "amount_micros, created_at) VALUES ($1,$2,$3,$4,$5,$6, clock_timestamp())",
                                reservation_id, request_id, agent_id, mandate_id, model, amount)
        except _GuardFailed as failed:
            return failed.args[0]
    return None


async def settle(pool, reservation_id: uuid.UUID, outcome: str, cost: int | None, prompt_tokens: int | None = None,
                 completion_tokens: int | None = None) -> dict | None:
    """Close a held reservation. outcome: "settled" (charge `cost`; None = charge the reserved amount)
    or "released" (charge 0). Touches the ledger only if this call is the one that closed it, so a late
    settle after the sweeper is a no-op (returns None). Returns the closed reservation."""
    if outcome == "released":
        cost = 0
    async with pool.acquire() as c, c.transaction():
        row = await c.fetchrow(
            "UPDATE reservations SET status=$2, cost_micros=coalesce($3, amount_micros), settled_at=now() "
            "WHERE request_id=$1 AND status='held' "
            "RETURNING client_request_id, agent_id, mandate_id, model, amount_micros, cost_micros", reservation_id, outcome, cost)
        if not row:
            return None
        for table, id_ in (("agents", row["agent_id"]), ("mandates", row["mandate_id"])):
            await c.execute(f"UPDATE {table} SET spent_micros = {MONTH_SPENT} + $3, period = budget_period(), "
                            f"reserved_micros = reserved_micros - $2 WHERE id=$1",
                            id_, row["amount_micros"], row["cost_micros"])
        if outcome == "settled":
            await c.execute(
                "INSERT INTO usage_monthly AS u (month, agent_id, mandate_id, model, requests, prompt_tokens, "
                "completion_tokens, cost_micros) VALUES (budget_period(), $1, $2, $3, 1, $4, $5, $6) "
                "ON CONFLICT (month, agent_id, mandate_id, model) DO UPDATE SET requests = u.requests + 1, "
                "prompt_tokens = u.prompt_tokens + $4, completion_tokens = u.completion_tokens + $5, "
                "cost_micros = u.cost_micros + $6",
                row["agent_id"], row["mandate_id"], row["model"], prompt_tokens or 0, completion_tokens or 0,
                row["cost_micros"])
        return dict(row)


async def sweep(pool, older_than_seconds: int | None) -> int:
    """Charge abandoned reservations (gateway crash, lost task) at their reserved amount.
    older_than_seconds=None sweeps every held row (startup; assumes a single gateway replica)."""
    # ponytail: single replica; tag reservations with an instance id before running several gateways.
    ids = await pool.fetch(
        "SELECT request_id FROM reservations WHERE status='held' "
        "AND ($1::int IS NULL OR created_at < now() - make_interval(secs => $1))", older_than_seconds)
    for r in ids:  # one transaction per reservation
        row = await settle(pool, r["request_id"], "settled", None)
        if row:
            await record_usage(pool, request_id=row["client_request_id"] or r["request_id"], agent_id=row["agent_id"],
                               mandate_id=row["mandate_id"], model_requested=row["model"], outcome="error",
                               reason_code="ABANDONED", cost_micros=row["cost_micros"], cost_source="reservation")
    return len(ids)


async def cleanup(pool, retention_days: int) -> tuple[int, int]:
    """Delete raw usage rows past retention and closed reservations older than 2 days.
    usage_monthly (the totals) is never deleted."""
    usage = await pool.execute("DELETE FROM usage WHERE at < now() - make_interval(days => $1)", retention_days)
    res = await pool.execute("DELETE FROM reservations WHERE status <> 'held' AND settled_at < now() - interval '2 days'")
    return int(usage.split()[-1]), int(res.split()[-1])
