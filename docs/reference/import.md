# Project import

The master importer loads a whole project folder (`project.yaml`,
`subjects.csv`, `visits.csv`, `samples.csv`, `files/manifest.csv`)
into the database in a single transaction. See the [CLI page](../cli.md)
for the command-line wrapper.

## Top-level entry point

::: dbmaria_utils._import

## Runner

::: dbmaria_utils._import.runner

## Loader

::: dbmaria_utils._import.loader

## Schema

::: dbmaria_utils._import.schema
