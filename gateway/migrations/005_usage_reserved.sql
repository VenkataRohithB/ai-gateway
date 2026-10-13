-- Show the worst-case hold next to the real charge for every request (sum over fallback attempts).
ALTER TABLE usage ADD COLUMN reserved_micros bigint NOT NULL DEFAULT 0;
