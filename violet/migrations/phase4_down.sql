-- Phase 4 rollback. Run ONLY after backing up violet.db and after deploying the
-- phase-3 code (git checkout 42ac28b). Phase 1-3 tables are not touched.
-- Phase-3 code ignores these tables, so dropping them is optional.
BEGIN;
DROP TABLE IF EXISTS consistency_flags;
DROP TABLE IF EXISTS life_engine_runs;
DROP TABLE IF EXISTS life_revisions;
DROP TABLE IF EXISTS life_events;
DROP TABLE IF EXISTS life_plans;
DROP TABLE IF EXISTS life_threads;
DROP TABLE IF EXISTS persona_dynamic;
DROP TABLE IF EXISTS persona_core_versions;
COMMIT;
