-- Phase 5B rollback. Back up violet.db first and deploy the phase-5A code
-- (git checkout 9be0f7e). Phase 1-5A tables are not touched; phase-5A code ignores these tables.
-- Reading-record life events written by the explorer stay in life_events (event_type 'reading_record');
-- remove them only if wanted: DELETE FROM life_events WHERE event_type = 'reading_record';
BEGIN;
DROP TABLE IF EXISTS web_requests;
DROP TABLE IF EXISTS web_sources;
DROP TABLE IF EXISTS web_findings;
COMMIT;
