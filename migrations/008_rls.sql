-- The browser has no direct table access during the initial migration.
-- FastAPI connects with the trusted server database role. Enabling RLS without
-- policies denies PostgREST access through anon/authenticated roles by default.

ALTER TABLE draws ENABLE ROW LEVEL SECURITY;
ALTER TABLE draw_stages ENABLE ROW LEVEL SECURITY;
ALTER TABLE draw_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE holder_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE ticket_ownership_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE draw_rounds ENABLE ROW LEVEL SECURITY;
ALTER TABLE round_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_job_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchase_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE draws FROM anon, authenticated;
REVOKE ALL ON TABLE draw_stages FROM anon, authenticated;
REVOKE ALL ON TABLE draw_participants FROM anon, authenticated;
REVOKE ALL ON TABLE holder_credentials FROM anon, authenticated;
REVOKE ALL ON TABLE tickets FROM anon, authenticated;
REVOKE ALL ON TABLE ticket_ownership_events FROM anon, authenticated;
REVOKE ALL ON TABLE draw_rounds FROM anon, authenticated;
REVOKE ALL ON TABLE round_results FROM anon, authenticated;
REVOKE ALL ON TABLE import_batches FROM anon, authenticated;
REVOKE ALL ON TABLE import_rows FROM anon, authenticated;
REVOKE ALL ON TABLE email_batches FROM anon, authenticated;
REVOKE ALL ON TABLE email_jobs FROM anon, authenticated;
REVOKE ALL ON TABLE email_job_tickets FROM anon, authenticated;
REVOKE ALL ON TABLE listings FROM anon, authenticated;
REVOKE ALL ON TABLE purchase_requests FROM anon, authenticated;
REVOKE ALL ON TABLE trades FROM anon, authenticated;
