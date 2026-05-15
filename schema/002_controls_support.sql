-- =========================================================
-- Migration: 002_controls_support
-- Description: Allow NULL sex/age for control samples
--              (anchors, mocks, NCs, inputs); add NC to
--              sample_type ENUM.
--
-- Author: Gabriel Innocenti, Mateusz F. Kołek
-- Created: 2026-05-15
-- Version: 2.0
--
-- Compatible with:
--   - MariaDB >= 10.x
--   - InnoDB storage engine
--   - Galera cluster
--
-- Notes:
-- subjects.sex: NULL now permitted; non-null values still
--   restricted to 'M' or 'F'.
-- visits.age: NULL now permitted; non-null values still
--   restricted to >= 0.
-- samples.sample_type: 'NC' added to the ENUM.
--
-- How to run:
--   mysql -u <user> -p dbmaria_project < 002_controls_support.sql
--   OR inside MariaDB:
--   SOURCE 002_controls_support.sql;
--
-- =========================================================

USE dbmaria_project;

-- ---------------------------------------------------------
-- subjects: allow NULL sex, keep 'M'/'F' check on non-null
-- ---------------------------------------------------------
ALTER TABLE subjects
    MODIFY sex CHAR(1) NULL,
    DROP CONSTRAINT chk_subjects_sex,
    ADD CONSTRAINT chk_subjects_sex CHECK (sex IS NULL OR sex IN ('M', 'F'));

-- ---------------------------------------------------------
-- visits: allow NULL age, keep >= 0 check on non-null
-- ---------------------------------------------------------
ALTER TABLE visits
    MODIFY age INT NULL,
    DROP CONSTRAINT chk_visits_age,
    ADD CONSTRAINT chk_visits_age CHECK (age IS NULL OR age >= 0);

-- ---------------------------------------------------------
-- samples: add 'NC' to sample_type ENUM
-- ---------------------------------------------------------
ALTER TABLE samples
    MODIFY sample_type ENUM('sample', 'mockIP', 'input', 'anchor', 'NC') NOT NULL;
