"""Journalisation centralisee, compatible cron.

Objectifs :

* un seul format de ligne, lisible par `grep` et par un ordonnanceur ;
* un `run_id` present sur chaque ligne, pour coreler les 19 etapes ;
* le masquage systematique des secrets, applique au formatage ;
* des fichiers de log en `0600` et un repertoire en `0700` ;
* aucune dependance a un terminal, donc utilisable sous cron.

La sortie standard reste reservee au rapport humain et au code de sortie :
les diagnostics vont sur stderr, ce qui permet de rediriger les deux
independamment.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .redact import redact

LOGGER_NAME = "osd"

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


class RedactingFormatter(logging.Formatter):
    """Formateur qui masque les secrets apres interpolation."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _secure_dir(path: Path, mode: int = 0o700) -> None:
    """Cree un repertoire avec des permissions restrictives.

    `exist_ok=True` n'ecrase pas les permissions d'un repertoire deja
    present : on les corrige explicitement, parce qu'un `logs/` cree par un
    umask laxiste contiendrait des rapports exploitables par d'autres.
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:  # pragma: no cover - systeme de fichiers exotique
        pass


def setup(
    log_dir: Path,
    level: str = "INFO",
    *,
    run_id: str = "-",
    to_stderr: bool = True,
    log_file: Optional[Path] = None,
) -> logging.Logger:
    """Configure et retourne le logger racine du projet.

    `log_dir` et le repertoire des logs. `log_file` permet de forcer un
    fichier (tests). Les handlers existants sont retires pour que
    l'appel soit idempotent.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if level.upper() not in _LEVELS:
        level = "INFO"
    threshold = _LEVELS[level.upper()]

    # Filtres appliques au logger : le contexte (run_id, etape) est injecte
    # avant tout formatage, le seuil evite d'ecrire une ligne inutile sur
    # stderr. Aucun niveau ne contourne ainsi le masque des secrets.
    logger.addFilter(_context)
    logger.addFilter(_ThresholdFilter(threshold))

    if log_file is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_file = log_dir / f"run-{run_id}-{stamp}.log"
    else:
        log_file = Path(log_file)

    _secure_dir(Path(log_dir))
    _secure_dir(log_file.parent)

    fmt = RedactingFormatter(
        fmt="%(asctime)s %(levelname)-7s [%(run_id)s] [%(stepname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    try:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disque plein / droits
        print(
            "osd: impossible d'ouvrir le journal %s: %s" % (log_file, exc),
            file=sys.stderr,
        )
        raise

    # 0600 : les rapports peuvent contenir des noms d'objets metier.
    try:
        os.chmod(log_file, 0o600)
    except OSError:  # pragma: no cover
        pass
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if to_stderr:
        err_handler = logging.StreamHandler(stream=sys.stderr)
        err_handler.setLevel(threshold)
        err_handler.setFormatter(fmt)
        logger.addHandler(err_handler)

    return logger


class _ThresholdFilter(logging.Filter):
    """Jette les enregistrements sous le seuil configure.

    Le logger est au niveau DEBUG (les handlers ecrivent au fichier) mais
    stderr ne doit recevoir que ce qui est reellement utile en session.
    """

    def __init__(self, threshold: int) -> None:
        super().__init__()
        self.threshold = threshold

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.threshold


class _ContextFilter(logging.Filter):
    """Injecte `run_id` et `stepname` dans chaque enregistrement.

    Un unique filtre detient l'etat de contexte plutot qu'un filtre par
    handler : `set_step` reste alors global et sans effet de bord selon
    l'ordre d'ajout des handlers.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self.step = "-"

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        record.stepname = self.step
        return True


#: Contexte partage, enregistre sur le logger par `get_logger`/`setup`.
_context = _ContextFilter("-")


def set_run_id(run_id: str) -> None:
    """Fixe le `run_id` utilise par les enregistrements suivants."""
    _context.run_id = run_id


def set_step(step: str) -> None:
    """Fixe l'etape courante, affichee entre crochets dans chaque ligne."""
    _context.step = step


def get_logger() -> logging.Logger:
    """Retourne le logger du projet, en installant le filtre de contexte
    de facon idempotente."""
    logger = logging.getLogger(LOGGER_NAME)
    if _context not in logger.filters:
        logger.addFilter(_context)
    return logger
