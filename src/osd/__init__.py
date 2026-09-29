"""Oracle Schema Duplicator — duplication de schemas Oracle 19c.

Outil d'orchestration execute sur un serveur de saut Linux (Python 3.9+),
pilant `expdp`/`impdp` sur des hotes AIX qui hebergent les bases. Le
serveur de saut ne de tient aucun identifiant Oracle : l'authentification
se fait par wallet sur chaque hote.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
