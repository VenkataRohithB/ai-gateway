-- db.reserve() locks the assignment row with SELECT ... FOR SHARE, which Postgres only allows
-- with UPDATE privilege. The table has no updatable business columns, so this grants nothing else.
GRANT UPDATE ON agent_mandates TO gateway_app;
