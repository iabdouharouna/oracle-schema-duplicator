"""Etapes du workflow de duplication.

`pipeline.py` contient l'orchestration des 19 etapes definies par
AGENTS.md. Ce package est son point d'entree.
"""

from .pipeline import STEP_INDEX, STEP_NAMES, STEPS, Pipeline  # noqa: F401

__all__ = ["Pipeline", "STEPS", "STEP_INDEX", "STEP_NAMES"]
