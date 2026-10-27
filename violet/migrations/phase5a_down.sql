-- Phase 5A rollback. Back up violet.db first and deploy the phase-4 code
-- (git checkout ffd1fae). Phase 1-4 tables are not touched; phase-4 code ignores these tables.
BEGIN;
DROP TABLE IF EXISTS life_settings;
DROP TABLE IF EXISTS aux_llm_calls;
COMMIT;
