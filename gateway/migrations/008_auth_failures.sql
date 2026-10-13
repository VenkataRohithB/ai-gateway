-- Count of requests with a missing/invalid agent token, one row per day (India time). Invalid-token requests are
-- not written to `usage` (anyone could flood it); a per-day counter is bounded however many arrive.
CREATE TABLE auth_failures (
  day   date PRIMARY KEY,
  count bigint NOT NULL DEFAULT 0
);
GRANT SELECT, INSERT, UPDATE ON auth_failures TO gateway_app;
