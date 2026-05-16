# noxDB

[![Project Status: WIP – Initial development is in progress.](https://www.repostatus.org/badges/latest/wip.svg)](https://www.repostatus.org/#wip)
[![CI](https://github.com/Polymerase3/noxdb/actions/workflows/ci.yml/badge.svg)](https://github.com/Polymerase3/noxdb/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/Polymerase3/noxDB/branch/main/graph/badge.svg)](https://codecov.io/gh/Polymerase3/noxDB)
[![version](https://img.shields.io/badge/version-0.7.0-blue)](./NEWS.md)
[![docs](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://polymerase3.github.io/noxDB/)

`noxdb` is the lab's MariaDB metadata database and the Python package that talks to it. It stores metadata and file pointers for biological samples — subjects, visits, samples, and where the resulting data files live on disk. The `subject → visit → sample → files` chain is pure lineage; project membership is a separate many-to-many relation (`project_samples`), so a sample can belong to several projects.

**Full documentation:** <https://polymerase3.github.io/noxDB/>

---

## Where to go next

- **[Quickstart](https://polymerase3.github.io/noxDB/quickstart/)** — connecting, querying, and fetching data
- **[Schema](https://polymerase3.github.io/noxDB/schema/)** — table layout and controls design
- **[API reference](https://polymerase3.github.io/noxDB/reference/)** — every public function
- **[Install](https://polymerase3.github.io/noxDB/install/)** — prerequisites and credential setup

---

## Contact

- Schema, DB admin, access: **Mateusz Franciszek Kołek** — <mateusz.kolek@meduniwien.ac.at>
- Co-maintainer: **Gabriel Innocenti** — <gabriel.innocenti@meduniwien.ac.at>
- Bugs / feature requests: [GitHub issues](https://github.com/Polymerase3/noxdb/issues)
