-- Phase 6 rollback. Back up violet.db AND the media directory first, then deploy the
-- phase-5B code (git checkout 70d0ecd). Phase 1-5B tables are not touched.
-- Image files under VIOLET_MEDIA_DIR are not deleted by this script; remove that directory by hand if wanted.
BEGIN;
DROP TABLE IF EXISTS visual_profile_versions;
DROP TABLE IF EXISTS image_capability_checks;
DROP TABLE IF EXISTS image_model_settings;
DROP TABLE IF EXISTS image_calls;
DROP TABLE IF EXISTS image_tasks;
DROP TABLE IF EXISTS media_assets;
COMMIT;
