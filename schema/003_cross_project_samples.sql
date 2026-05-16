-- =========================================================
-- Migration: 003_cross_project_samples
-- Description: Make project↔sample membership many-to-many.
--              A new `project_samples` junction becomes the
--              SOLE link between projects and samples. The
--              subject → visit → sample chain is now pure
--              lineage and carries no project affiliation.
--              Control projects (mockIP, anchor, NC) are
--              deleted; their samples stay and are linked to
--              study projects via SQR+SQRP.
--
-- Author: Mateusz F. Kołek
-- Created: 2026-05-16
-- Version: 3.0
--
-- Compatible with:
--   - MariaDB >= 10.x
--   - InnoDB storage engine
--   - Galera cluster
--
-- Notes:
-- subjects.project_id is dropped. subject_code becomes
--   globally unique (pre-flight must confirm zero duplicate
--   subject_codes across projects before running this).
-- project_samples is the only source of truth for which
--   samples belong to which project.
-- Controls are backfilled under every study project that
--   shares their SQR+SQRP plate coordinates.
--
-- How to run (PRODUCTION database is `ccr_metadata`; the dev
-- database is `dbmaria_project` — change the USE below for dev):
--   mysql -u <user> -p ccr_metadata < 003_cross_project_samples.sql
--   OR inside MariaDB:
--   SOURCE 003_cross_project_samples.sql;
--
-- Pre-flight (must return 0 rows — abort otherwise):
--   SELECT subject_code, COUNT(*) FROM subjects
--   GROUP BY subject_code HAVING COUNT(*) > 1;
--
-- =========================================================

USE ccr_metadata;

-- ---------------------------------------------------------
-- Canonicalize SQR/SQRP before anything matches on them.
-- Plate identifiers are matched by exact string equality
-- (Backfill 3 below, the importer's control auto-link, and
-- every runtime query). Collapse whitespace and the
-- 'absent' sentinels (NA / N/A / empty) to a single
-- canonical empty string so the match can't silently miss.
-- Zero-padding (e.g. '01') is intentionally preserved.
-- This mirrors noxdb.samples.canonical_plate_id().
-- ---------------------------------------------------------
UPDATE samples SET SQR = TRIM(SQR), SQRP = TRIM(SQRP);
UPDATE samples SET SQR  = '' WHERE LOWER(SQR)  IN ('na', 'n/a');
UPDATE samples SET SQRP = '' WHERE LOWER(SQRP) IN ('na', 'n/a');

-- ---------------------------------------------------------
-- Junction table: sole source of project↔sample membership
-- ---------------------------------------------------------
CREATE TABLE project_samples (
    project_id BIGINT UNSIGNED NOT NULL,
    sample_id  BIGINT UNSIGNED NOT NULL,

    PRIMARY KEY (project_id, sample_id),

    CONSTRAINT fk_ps_project
        FOREIGN KEY (project_id)
        REFERENCES projects(project_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    CONSTRAINT fk_ps_sample
        FOREIGN KEY (sample_id)
        REFERENCES samples(sample_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    KEY idx_ps_sample_id (sample_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------
-- Backfill 1: study samples → their study projects
-- (runs while subjects.project_id still exists)
-- ---------------------------------------------------------
INSERT INTO project_samples (project_id, sample_id)
SELECT s.project_id, sm.sample_id
FROM samples sm
JOIN visits v   ON v.visit_id   = sm.visit_id
JOIN subjects s ON s.subject_id = v.subject_id
WHERE s.project_id NOT IN (
    SELECT project_id FROM projects
    WHERE project_name IN ('mockIP', 'anchor', 'NC', 'input')
);

-- ---------------------------------------------------------
-- Backfill 2: input samples → input project
-- ---------------------------------------------------------
INSERT INTO project_samples (project_id, sample_id)
SELECT s.project_id, sm.sample_id
FROM samples sm
JOIN visits v   ON v.visit_id   = sm.visit_id
JOIN subjects s ON s.subject_id = v.subject_id
WHERE s.project_id IN (
    SELECT project_id FROM projects WHERE project_name = 'input'
);

-- ---------------------------------------------------------
-- Backfill 3: controls → every study project sharing the
-- same SQR+SQRP plate coordinates
-- ---------------------------------------------------------
INSERT IGNORE INTO project_samples (project_id, sample_id)
SELECT DISTINCT sp.project_id, ctrl_sm.sample_id
FROM samples ctrl_sm
JOIN visits   ctrl_v   ON ctrl_v.visit_id     = ctrl_sm.visit_id
JOIN subjects ctrl_sub ON ctrl_sub.subject_id = ctrl_v.subject_id
JOIN projects ctrl_p   ON ctrl_p.project_id   = ctrl_sub.project_id
    AND ctrl_p.project_name IN ('mockIP', 'anchor', 'NC')
JOIN samples study_sm  ON study_sm.SQR  = ctrl_sm.SQR
    AND study_sm.SQRP = ctrl_sm.SQRP
    AND study_sm.sample_type = 'sample'
JOIN visits   study_v   ON study_v.visit_id     = study_sm.visit_id
JOIN subjects study_sub ON study_sub.subject_id = study_v.subject_id
JOIN projects sp        ON sp.project_id        = study_sub.project_id
    AND sp.project_name NOT IN ('mockIP', 'anchor', 'NC', 'input');

-- ---------------------------------------------------------
-- subjects: drop project_id and its dependent keys/FK
-- ---------------------------------------------------------
ALTER TABLE subjects
    DROP FOREIGN KEY fk_subjects_project,
    DROP KEY idx_subjects_project_id,
    DROP KEY uq_subjects_project_subject_code,
    DROP COLUMN project_id;

-- subjects are now globally unique by subject_code
ALTER TABLE subjects
    ADD UNIQUE KEY uq_subjects_subject_code (subject_code);

-- ---------------------------------------------------------
-- Delete control projects. CASCADE clears any project_samples
-- rows that referenced these project_ids — harmless, because
-- Backfill 3 linked controls under study project_ids only.
-- ---------------------------------------------------------
DELETE FROM projects WHERE project_name IN ('mockIP', 'anchor', 'NC');
