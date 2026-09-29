"""Hierarchie d'exceptions et correspondance avec les codes de sortie.

Regle de conception : une exception porte toujours son code de sortie. Le
`cli.py` se contente alors de `except OsdError as e: return e.code`, ce
qui garantit qu'aucun chemin d'erreur ne peut se terminer avec le code 0
(succes) par oubli.
"""

from __future__ import annotations

from typing import Optional, Sequence

from . import exit_codes as ec


class OsdError(Exception):
    """Erreur metier portant son code de sortie normalise."""

    def __init__(
        self,
        message: str,
        code: int,
        *,
        step: Optional[str] = None,
        detail: Optional[Sequence[str]] = None,
        hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        if not ec.is_valid(code):
            raise ValueError(f"code de sortie invalide: {code!r}")
        self.message = message
        self.code = code
        self.step = step
        self.detail = tuple(detail or ())
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message

    def as_dict(self) -> dict:
        """Representation serialisable, utilisee par le rapport JSON."""
        return {
            "message": self.message,
            "code": self.code,
            "code_label": ec.label(self.code),
            "step": self.step,
            "detail": list(self.detail),
            "hint": self.hint,
        }


class ConfigError(OsdError):
    """Configuration absente, illisible, inconnue ou hors enumeration."""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, ec.CONFIG, **kw)


class PrereqError(OsdError):
    """Dependance manquante, etat du schema/tablespace incompatible."""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, ec.PREREQ, **kw)


class ConnectionError_(OsdError):
    """Connexion source ou cible impossible (reseau, listener, wallet)."""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, ec.CONNECTION, **kw)


class ExportError(OsdError):
    """Echec de l'export Data Pump ou verification du dump."""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, ec.EXPORT, **kw)


class TransferError(OsdError):
    """Echec de copie du dump vers l'hote cible."""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, ec.TRANSFER, **kw)


class ImportError_(OsdError):
    """Echec de l'import Data Pump."""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, ec.IMPORT, **kw)


class ValidationError(OsdError):
    """Ecart source/cible, objets invalides, ou etat final incorrect."""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, ec.VALIDATION, **kw)


class SecurityError(OsdError):
    """Garde-fou refuse : operation destructive non autorisee, secret
    expose dans un fichier trop permissif, etc."""

    def __init__(self, message: str, **kw) -> None:
        super().__init__(message, ec.SECURITY, **kw)


class Interrupted(OsdError):
    """Interruption par SIGINT/SIGTERM."""

    def __init__(self, message: str = "interruption", **kw) -> None:
        super().__init__(message, ec.INTERRUPTED, **kw)
