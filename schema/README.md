# Schema migrations

Numbered SQL files, applied in order. Once applied to a deployed database, a migration file is **immutable** — fix mistakes by adding a new migration, never by editing an old one.

## Naming

`NNN_short_description.sql` — zero-padded, 3 digits.

- `001_initial.sql` — initial schema
- `002_*.sql`, `003_*.sql`, ... — subsequent changes

## Applying

```sh
mysql -u <admin_user> -p < 001_initial.sql
```

Or from inside MariaDB:

```sql
SOURCE 001_initial.sql;
```

## Tracking

A `schema_migrations` table records which files have been applied. See `docs/migrations.md` (TBD).
