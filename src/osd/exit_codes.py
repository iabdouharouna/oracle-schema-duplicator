"""Codes de sortie normalises (voir docs/EXIT_CODES.md).

Table unique de verite partagee par la CLI, la machine a etats et le
reporting. Toute erreur doit se mapper sur exactement un code.

    0 = succes
    1 = configuration
    2 = prerequis
    3 = connexion
    4 = export
    5 = transfert
    6 = import
    7 = validation
    8 = securite
    9 = interruption
"""

from __future__ import annotations

from typing import Dict, Final

SUCCESS: Final[int] = 0
CONFIG: Final[int] = 1
PREREQ: Final[int] = 2
CONNECTION: Final[int] = 3
EXPORT: Final[int] = 4
TRANSFER: Final[int] = 5
IMPORT: Final[int] = 6
VALIDATION: Final[int] = 7
SECURITY: Final[int] = 8
INTERRUPTED: Final[int] = 9

#: Libelle lisible, utilise par le rapport texte et la documentation.
LABELS: Final[Dict[int, str]] = {
    SUCCESS: "succes",
    CONFIG: "configuration invalide",
    PREREQ: "prerequis non satisfaits",
    CONNECTION: "connexion impossible",
    EXPORT: "echec de l'export",
    TRANSFER: "echec du transfert",
    IMPORT: "echec de l'import",
    VALIDATION: "echec de la validation",
    SECURITY: "garde-fou de securite refuse",
    INTERRUPTED: "interruption",
}

#: Bornes inclusives acceptees, pour validation stricte.
VALID: Final[frozenset] = frozenset(LABELS)


def label(code: int) -> str:
    """Retourne le libelle lisible d'un code de sortie.

    Un code inconnu ne doit jamais faire echouer l'affichage du rapport :
    il degrade proprement vers une mention explicite.
    """
    return LABELS.get(code, "code inconnu")


def is_valid(code: int) -> bool:
    """Indique si le code appartient a la table normalisee."""
    return code in VALID
