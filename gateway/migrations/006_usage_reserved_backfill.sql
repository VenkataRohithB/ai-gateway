-- Fill the hold for requests recorded before 005, from the reservations still kept (last ~2 days).
UPDATE usage u SET reserved_micros = r.held
FROM (SELECT coalesce(client_request_id, request_id) AS rid, sum(amount_micros) AS held FROM reservations GROUP BY 1) r
WHERE u.request_id = r.rid AND u.reserved_micros = 0;
