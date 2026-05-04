-- =========================================================
-- Database Schema: dbmaria_project
-- Description: Relational schema for project, subject,
--              visit, and sample metadata storage
--
-- Author: Gabriel Innocenti, Mateusz F. Kołek
-- Created: 2026-04-29
-- Version: 1.0
--
-- Compatible with:
--   - MariaDB >= 10.x
--   - InnoDB storage engine
--   - Galera cluster
--
-- Notes:
-- Hierarchical structure: project → subject → visit → sample
--   - Projects define independent datasets/studies
--   - Subjects store stable subject-level attributes
--   - Visit table stores timepoint-level metadata
--   - Sample table stores technical metadata
--   - Additional flexible metadata stored in sample_metadata
--
-- How to run:
--   mysql -u <user> -p < schema.sql
--   OR inside MariaDB:
--   SOURCE schema.sql;
--
-- =========================================================

CREATE DATABASE IF NOT EXISTS dbmaria_project
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE dbmaria_project;

-- =========================================================
-- Table 1: projects
-- One row per project
-- =========================================================
CREATE TABLE projects (
    project_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    project_name VARCHAR(100) NOT NULL,
    description TEXT NULL,
    pi_name VARCHAR(100) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id),
    UNIQUE KEY uq_projects_project_name (project_name)
) ENGINE=InnoDB;


-- =========================================================
-- Table 2: subjects
-- One row per subject/person within a project
-- Stable subject-level information only
-- =========================================================
CREATE TABLE subjects (
    subject_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    project_id BIGINT UNSIGNED NOT NULL,
    subject_code VARCHAR(100) NOT NULL,
    sex CHAR(1) NOT NULL,
    origin VARCHAR(100) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (subject_id),

    CONSTRAINT fk_subjects_project
        FOREIGN KEY (project_id)
        REFERENCES projects(project_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    CONSTRAINT chk_subjects_sex
        CHECK (sex IN ('M', 'F')),

    UNIQUE KEY uq_subjects_project_subject_code (project_id, subject_code),
    KEY idx_subjects_project_id (project_id),
    KEY idx_subjects_subject_code (subject_code)
) ENGINE=InnoDB;


-- =========================================================
-- Table 3: visits
-- One row per subject × timepoint / visit / collection event
-- Time-varying biological or clinical metadata belongs here
-- =========================================================
CREATE TABLE visits (
    visit_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    subject_id BIGINT UNSIGNED NOT NULL,
    timepoint VARCHAR(50) NULL,
    group_test VARCHAR(100) NOT NULL,
    age INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (visit_id),

    CONSTRAINT fk_visits_subject
        FOREIGN KEY (subject_id)
        REFERENCES subjects(subject_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    CONSTRAINT chk_visits_age
        CHECK (age >= 0),

    UNIQUE KEY uq_visits_subject_timepoint (subject_id, timepoint),
    KEY idx_visits_subject_id (subject_id),
    KEY idx_visits_timepoint (timepoint),
    KEY idx_visits_group_test (group_test)
) ENGINE=InnoDB;


-- =========================================================
-- Table 4: samples
-- One row per actual sample / well / library / Ig class measurement
-- =========================================================
CREATE TABLE samples (
    sample_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    visit_id BIGINT UNSIGNED NOT NULL,
    sample_name VARCHAR(100) NOT NULL,
    SQR VARCHAR(10) NOT NULL,
    SQRP VARCHAR(10) NOT NULL,
    library VARCHAR(50) NOT NULL,
    antibody_class VARCHAR(50) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (sample_id),

    CONSTRAINT fk_samples_visit
        FOREIGN KEY (visit_id)
        REFERENCES visits(visit_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    UNIQUE KEY uq_samples_sample_name (sample_name),
    KEY idx_samples_visit_id (visit_id),
    KEY idx_samples_sqr (SQR),
    KEY idx_samples_sqrp (SQRP),
    KEY idx_samples_library (library),
    KEY idx_samples_antibody_class (antibody_class)
) ENGINE=InnoDB;


-- =========================================================
-- Table 5: visit_metadata
-- Flexible metadata attached to a visit/timepoint
-- e.g. BMI, smoker status, disease activity, treatment status
-- =========================================================
CREATE TABLE visit_metadata (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    visit_id BIGINT UNSIGNED NOT NULL,
    key_name VARCHAR(100) NOT NULL,

    value_int INTEGER NULL,
    value_numeric DECIMAL(20,6) NULL,
    value_bool BOOLEAN NULL,
    value_text TEXT NULL,

    value_type ENUM('int', 'numeric', 'bool', 'text') NOT NULL,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),

    CONSTRAINT fk_visit_metadata_visit
        FOREIGN KEY (visit_id)
        REFERENCES visits(visit_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    CONSTRAINT chk_visit_metadata_value_type
        CHECK (
            (value_type = 'int'
             AND value_int IS NOT NULL
             AND value_numeric IS NULL
             AND value_bool IS NULL
             AND value_text IS NULL)

            OR

            (value_type = 'numeric'
             AND value_int IS NULL
             AND value_numeric IS NOT NULL
             AND value_bool IS NULL
             AND value_text IS NULL)

            OR

            (value_type = 'bool'
             AND value_int IS NULL
             AND value_numeric IS NULL
             AND value_bool IS NOT NULL
             AND value_text IS NULL)

            OR

            (value_type = 'text'
             AND value_int IS NULL
             AND value_numeric IS NULL
             AND value_bool IS NULL
             AND value_text IS NOT NULL)
        ),

    UNIQUE KEY uq_visit_metadata_visit_key (visit_id, key_name),
    KEY idx_visit_metadata_visit_id (visit_id),
    KEY idx_visit_metadata_key_name (key_name),
    KEY idx_visit_metadata_value_type (value_type)
) ENGINE=InnoDB;


-- =========================================================
-- Table 6: sample_metadata
-- Flexible metadata attached to a sample
-- e.g. well position, plate barcode, dilution factor
-- =========================================================
CREATE TABLE sample_metadata (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    sample_id BIGINT UNSIGNED NOT NULL,
    key_name VARCHAR(100) NOT NULL,
    value TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),

    CONSTRAINT fk_sample_metadata_sample
        FOREIGN KEY (sample_id)
        REFERENCES samples(sample_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    UNIQUE KEY uq_sample_metadata_sample_key (sample_id, key_name),
    KEY idx_sample_metadata_sample_id (sample_id),
    KEY idx_sample_metadata_key_name (key_name)
) ENGINE=InnoDB;
