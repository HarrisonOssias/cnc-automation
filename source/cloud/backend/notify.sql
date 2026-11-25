-- Live updates for the office app: Postgres LISTEN/NOTIFY.
--
-- Triggers send a small JSON message on channel `forge_live` whenever a row the office shows changes.
-- The backend LISTENs on one connection and pushes each message to open browsers over SSE
-- (backend/live.py), so the office never polls.
--
-- Triggers, not NOTIFY calls in Python, because two processes write these tables (the API and the
-- telemetry consumer) and a trigger catches every write, even one made by hand in psql.
-- NOTIFY is transactional: a message is only delivered when its transaction commits, and identical
-- messages in one transaction are sent once (reordering a queue sends one "jobs" message, not ten).
-- Payloads must stay under 8000 bytes; larger rows send "this changed" and the client refetches.
--
-- Installed by db.migrate() on every start; everything here is safe to run again.

-- A machine event, as stored by the telemetry consumer. Carries the row so the office can show it
-- (and time the hop from the Pi to here) without asking for it.
CREATE OR REPLACE FUNCTION forge_notify_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  payload text;
BEGIN
  payload := json_build_object(
    'kind', 'event', 'machine_id', NEW.machine_id, 'session_id', NEW.session_id,
    'event', json_build_object(
      'id', NEW.id, 'type', NEW.type, 'at', NEW.at, 'data', NEW.data, 'session_id', NEW.session_id,
      'stream_offset', NEW.stream_offset, 'received_at', NEW.received_at, 'notified_at', clock_timestamp()
    )
  )::text;
  IF octet_length(payload) > 7900 THEN
    payload := json_build_object('kind', 'event', 'machine_id', NEW.machine_id, 'truncated', true)::text;
  END IF;
  PERFORM pg_notify('forge_live', payload);
  RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER forge_live_event AFTER INSERT ON machine_events
  FOR EACH ROW EXECUTE FUNCTION forge_notify_event();

-- A part session changed (a part was made, the state changed, it closed). The whole row, minus the
-- tool table it started with, which the office fetches with the session when it needs it.
CREATE OR REPLACE FUNCTION forge_notify_session() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('forge_live', json_build_object(
    'kind', 'session', 'machine_id', NEW.machine_id, 'session', to_jsonb(NEW) - 'tools'
  )::text);
  RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER forge_live_session AFTER INSERT OR UPDATE ON part_sessions
  FOR EACH ROW EXECUTE FUNCTION forge_notify_session();

-- A machine's queue changed (job added, reordered, completed, canceled): the office refetches it.
CREATE OR REPLACE FUNCTION forge_notify_jobs() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('forge_live', json_build_object(
    'kind', 'jobs', 'machine_id', COALESCE(NEW.machine_id, OLD.machine_id)
  )::text);
  RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER forge_live_jobs AFTER INSERT OR UPDATE OR DELETE ON jobs
  FOR EACH ROW EXECUTE FUNCTION forge_notify_jobs();

-- A Pi went online or offline, or reported a new agent version.
CREATE OR REPLACE FUNCTION forge_notify_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.online IS NOT DISTINCT FROM OLD.online
     AND NEW.agent_version IS NOT DISTINCT FROM OLD.agent_version THEN
    RETURN NULL;  -- only last_seen_at moved; not worth a message
  END IF;
  PERFORM pg_notify('forge_live', json_build_object(
    'kind', 'status', 'machine_id', NEW.machine_id, 'online', NEW.online,
    'agent_version', NEW.agent_version, 'last_seen_at', NEW.last_seen_at
  )::text);
  RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER forge_live_status AFTER INSERT OR UPDATE ON machine_status
  FOR EACH ROW EXECUTE FUNCTION forge_notify_status();

-- A part got a new file (an upload, or a preview rendered from its STEP): queued jobs show its files.
CREATE OR REPLACE FUNCTION forge_notify_file() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('forge_live', json_build_object('kind', 'files', 'part_number', NEW.part_number)::text);
  RETURN NULL;
END $$;

CREATE OR REPLACE TRIGGER forge_live_file AFTER INSERT ON part_files
  FOR EACH ROW EXECUTE FUNCTION forge_notify_file();
