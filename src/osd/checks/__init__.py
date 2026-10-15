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
    check_table_filter,
    check_tablespaces,
    first_failure,
    human,
    worst,
)
from .schema import CreateSchemaPlan, plan_create_target_schema  # noqa: F401
from .table_filter import TableFilterPlan, plan_table_filter  # noqa: F401

__all__ = [
    "CreateSchemaPlan",
    "CheckResult",
    "TableFilterPlan",
    "plan_create_target_schema",
    "plan_table_filter",
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
    "check_table_filter",
    "check_tablespaces",
    "first_failure",
    "human",
    "worst",
]
