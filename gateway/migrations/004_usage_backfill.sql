-- Bring calls recorded before v3 (request + settle rows in audit_events) into the usage table,
-- so the console's call history and charts include them. Rows older than the retention period
-- are removed by the normal cleanup. Safe to run on an empty database.
INSERT INTO usage (at, request_id, agent_id, mandate_id, model_requested, model_served, outcome, reason_code,
                   http_status, attempts, prompt_tokens, completion_tokens, cost_micros, cost_source, policy_version)
SELECT r.at, r.request_id, r.agent_id, r.mandate_id, r.model,
       CASE WHEN s.upstream_status = 200 THEN r.model END,
       CASE WHEN r.decision = 'deny' THEN 'denied'
            WHEN s.upstream_status = 200 THEN 'ok'
            WHEN s.decision = 'settled' AND s.cost_source = 'reservation' THEN 'timeout'
            ELSE 'error' END,
       coalesce(r.reason_code, CASE WHEN s.decision = 'settled' AND s.cost_source = 'reservation' AND s.upstream_status IS DISTINCT FROM 200
                                    THEN 'UPSTREAM_TIMEOUT' END),
       s.upstream_status, CASE WHEN r.decision = 'allow' THEN 1 ELSE 0 END,
       s.prompt_tokens, s.completion_tokens, coalesce(s.cost_micros, 0), s.cost_source, r.policy_version
FROM audit_events r
LEFT JOIN audit_events s ON s.kind = 'settle' AND s.request_id = r.request_id AND s.decision IN ('settled', 'released')
WHERE r.kind = 'request' AND r.agent_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM usage u WHERE u.request_id = r.request_id);
