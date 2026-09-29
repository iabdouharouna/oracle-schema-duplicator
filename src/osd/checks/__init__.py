"""Controles prealables a la duplication.

Chaque controle est autonome et testable sans base ni reseau. Voir
`preflight.py` pour la justification de cette forme.
"""

from .preflight import (  # noqa: F401
    FAIL,
    OK,
    SKIP,
    WARN,
    CheckResult,
    check_connection,
    check_dependencies,
    check_privileges,
    check_source_schema,
    check_space,
    check_target_schema,
    check_tablespaces,
    first_failure,
    human,
    worst,
)

__all__ = [
    "CheckResult",
    "FAIL",
    "OK",
    "SKIP",
    "WARN",
    "check_connection",
    "check_dependencies",
    "check_privileges",
    "check_source_schema",
    "check_space",
    "check_target_schema",
    "check_tablespaces",
    "first_failure",
    "human",
    "worst",
]
