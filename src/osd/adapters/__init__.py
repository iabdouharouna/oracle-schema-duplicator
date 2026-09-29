"""Adaptateurs : frontieres vers le monde exterieur (Oracle, SSH, disque).

Chaque adaptateur ne connait que des entrees deja validees et ne
revele jamais de secret. Ils sont deliberement minces : la logique
metier vit dans `stages/` et `checks/`, ce qui les rend testables sans
base de donnees ni reseau.
"""

from .oracle import OracleAdapter, OracleSide  # noqa: F401
from .null import NullRunner  # noqa: F401

__all__ = ["OracleAdapter", "OracleSide", "NullRunner"]
