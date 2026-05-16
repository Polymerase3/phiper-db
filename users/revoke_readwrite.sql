-- =========================================================
-- One-time migration: revoke INSERT/UPDATE/DELETE from
-- lovro.trgovec-greif and melanie.prinzensteiner, reducing
-- them to SELECT-only like all other non-admin users.
--
-- Author: Mateusz F. Kołek
-- Created: 2026-05-15
--
-- WHY (schema 003 context)
-- ------------------------
-- Under the cross-project samples model (schema/003), project↔sample
-- membership lives ONLY in `project_samples`. Those links are written
-- and kept consistent by the importer (noxdb._import.runner) and the
-- migration backfill — never by hand. Ad-hoc INSERT/UPDATE/DELETE from
-- analyst accounts would create samples with no project_samples link
-- (invisible to every project-scoped query) or orphan controls. These
-- two accounts are therefore reduced to SELECT-only like all other
-- non-admin users; writes must go through the importer.
--
-- HOW TO APPLY
-- ------------
--   mysql -u root -p < users/revoke_readwrite.sql
--
-- NOTE: the production database is `ccr_metadata`; this script still
-- targets `dbmaria_project` (dev). Adjust the USE / grant scope below
-- to `ccr_metadata` before applying in production.
-- =========================================================

USE dbmaria_project;

REVOKE INSERT, UPDATE, DELETE ON dbmaria_project.*
    FROM 'lovro.trgovec-greif'@'lisc.%';

REVOKE INSERT, UPDATE, DELETE ON dbmaria_project.*
    FROM 'melanie.prinzensteiner'@'lisc.%';

FLUSH PRIVILEGES;
