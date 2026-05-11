"""Master import subpackage: project-folder → database in one transaction.

Public surface is :func:`runner.import_project_from_dir`. The
:mod:`scripts.import_project` CLI is a thin wrapper around it.
"""

from dbmaria_utils._import.runner import (
    ProjectImportError,
    import_project_from_dir,
)

__all__ = ["ProjectImportError", "import_project_from_dir"]
