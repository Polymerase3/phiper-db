# Contributing to phiper-db

Thanks for working on `phiper-db`. This document is the short version of
"how to make a change that will pass review and CI."

## Ground rules

- All in-file comments, docstrings, log messages, and identifiers are in
  **English**, regardless of the language used in issues or PRs.
- Public API behavior changes need a corresponding test.
- Migrations are append-only: never edit a numbered `schema/NNN_*.sql`
  file once it has been merged. Add the next number instead.
- Stored file paths must be absolute (a `CHECK` constraint enforces this).

## Dev setup

```bash
python3 -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\activate
pip install -e ".[test,analysis,docs]"
```

You also need `libmariadb-dev` (Linux) / the equivalent system package for
the `mariadb` Python driver to build. See
[Testing](docs/testing.md) for the full local DB setup.

## Branching and PRs

1. Branch from `main` (e.g. `feature/<short-name>` or `fix/<short-name>`).
2. Make your change. Keep diffs focused — one logical change per PR.
3. **Bump the version** in `pyproject.toml` (semver: patch for fixes,
   minor for additive features, major for breaking changes).
4. **Add a matching entry** at the top of `NEWS.md`:

   ```markdown
   ## [x.y.z] - YYYY-MM-DD

   ### Added
   - …

   ### Changed
   - …

   ### Fixed
   - …
   ```

   Both the version bump and the `NEWS.md` entry are required and
   enforced by `.github/workflows/pr-checks.yml`. A PR without them
   will not pass CI.

5. Open a PR against `main`. Keep the title short and imperative
   (`Add fetch.export_project`, `Fix autocommit reset after pool checkout`).

## Tests

Run the full suite against a real MariaDB before pushing:

```bash
DB_HOST=127.0.0.1 DB_PORT=3306 DB_USER=root DB_PASSWORD=rootpw \
DB_NAME=dbmaria_project_test pytest -v
```

See [`docs/testing.md`](docs/testing.md) for the Docker recipe. The CI
job runs the same command against a `mariadb:10.11` service container.

## Docstring style

All public functions use **Google-style** docstrings so `mkdocstrings`
can render Args / Returns / Raises sections on the docs site:

```python
def get_or_create(cur, project_name: str, **kwargs) -> int:
    """Return the project_id, inserting the row if it does not exist.

    Args:
        cur: Audit-logging cursor from `transaction()`.
        project_name: Unique name of the project.
        **kwargs: Passed through to `create()` on insert.

    Returns:
        The `project_id` of the existing or newly inserted row.

    Raises:
        mariadb.IntegrityError: If a concurrent insert wins the race
            after the SELECT returned no row.
    """
```

The first line is a one-sentence summary. Cross-references to other
functions render automatically — just write `[get][dbmaria_utils.projects.get]`
in narrative paragraphs.

## Docs

The docs site is built with MkDocs + Material + mkdocstrings. Preview
locally:

```bash
pip install -e ".[docs]"
mkdocs serve              # http://127.0.0.1:8000
mkdocs build --strict     # the same check CI runs
```

Source files live under `docs/`. The site is published to GitHub Pages
on every push to `main` by `.github/workflows/docs.yml`.

## Reporting bugs / requesting features

Open a GitHub issue. For schema or access questions, email Mateusz
Kołek (see `README.md`).
