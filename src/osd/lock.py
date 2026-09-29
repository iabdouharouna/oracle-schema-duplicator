"""Verrou d'execution concurrente.

AGENTS.md impose de tester la concurrence. Deux executions simultanees
du meme couple source/cible seraient corruptrices : elles ecraseraient le
meme repertoire de travail, le meme fichier d'etat et le meme dump.

Le verrou est volontairement **hors du repertoire de travail par defaut** :
un `WORK_DIR` monte en NFS offre des semantiques de verrou peu fiables
selon le systeme de fichiers, alors qu'un `LOCK_DIR` local est toujours
correct. La cle est derivee du couple source/cible, pas d'un nom fixe :
deux duplications sans rapport doivent pouvoir tourner en meme temps.
"""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import time
from pathlib import Path
from typing import Optional

from .errors import OsdError
from . import exit_codes as ec

#: Duree au-dela de laquelle un verrou est considere comme perime. Un
#: processus encore vivant est toujours detecte par `kill(pid, 0)`.
STALE_AFTER_S = 24 * 3600

try:  # pragma: no cover - disponible partout ou le module est compile
    import fcntl
    _HAVE_FCNTL = hasattr(fcntl, "lockf")
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False


def lock_key(cfg) -> str:
    """Cle stable et lisible, derivee du couple source/cible.

    Le hachage evite tout caractere indesirable dans un nom de fichier,
    le prefixe lisible permet a un operateur d'identifier un verrou
    bloque dans `/tmp` sans ouvrir le fichier.
    """
    raw = f"{cfg.get('SOURCE_CONNECT')}|{cfg.get('SOURCE_SCHEMA')}|" \
          f"{cfg.get('TARGET_CONNECT')}|{cfg.get('TARGET_SCHEMA')}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    label = f"{cfg.get('SOURCE_SCHEMA')}to{cfg.get('TARGET_SCHEMA')}"
    safe = "".join(ch if ch.isalnum() else "-" for ch in label)[:24]
    return f"osd-{safe}-{digest}"


class Lock:
    """Verrou consultatif sur un fichier, libere a la destruction.

    Utilise comme gestionnaire de contexte ; la liberation est tentee
    meme en cas d'exception, ce qui compte pour les signaux SIGINT/SIGTERM
    qui lèvent une exception.
    """

    def __init__(self, path: Path, *, description: str = "") -> None:
        self.path = Path(path)
        self.description = description
        self._fd: Optional[int] = None
        self._method = "none"

    def __enter__(self) -> "Lock":
        self._preparer_repertoire()
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:  # pragma: no cover
            pass

        if not self._acquire_fcntl() and not self._acquire_mkdir():
            raise OsdError(
                "une duplication identique est deja en cours "
                f"({self.holder_text()})",
                ec.PREREQ,
                detail=[f"verrou: {self.path}"],
                hint="Attendre la fin de l'execution en cours, ou utiliser "
                     "un LOCK_DIR different pour un couple source/cible distinct.",
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _preparer_repertoire(self) -> None:
        """Cree le repertoire du verrou, ou explique pourquoi il est impossible.

        L'`OSError` laissait remonter jusqu'ici se lisait « erreur interne »
        cote CLI, donc code 1 : l'exploitant cherchait une faute de frappe
        dans la configuration alors que le probleme etait le systeme de
        fichiers. Les deux causes sont realistes et relevent de
        l'environnement, pas du programme : un `LOCK_DIR` pointe vers un
        fichier, un montage NFS en lecture seule, un quota epuise. Le
        code 2 les dit mieux que le code 1, et surtout ne renvoie pas
        l'exploitant vers la mauvaise piste.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OsdError(
                f" repertoire de verrou inutilisable : {self.path.parent} "
                f"({exc.strerror})",
                ec.PREREQ,
                detail=[f"verrou: {self.path}"],
                hint="Verifier que LOCK_DIR designe un repertoire "
                     "inscriptible, ou corriger sa valeur. "
                     "LOCK_DIR peut aussi rester vide : WORK_DIR est "
                     "alors utilise, avec les limites de verrouillage "
                     "d'un systeme de fichiers distribue.",
            ) from None

    # -- Strategies -------------------------------------------------------
    _holder: Optional[dict] = None
    _definitively_held = False

    def _acquire_fcntl(self) -> bool:
        """Verrou POSIX `lockf`, en mode non bloquant.

        Distingue trois situations : indisponible (on tente le repli),
        deja detenu par un tiers (etat definitif : le repli `mkdir`
        echouerait aussi et parlerait a tort d'un repertoire residuel),
        et acquis. Le mode non bloquant permet un message d'erreur
        exploitable au lieu d'une attente silencieuse sous cron.
        """
        if not _HAVE_FCNTL:
            return False
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                self._holder = self._read_holder()
                self._definitively_held = True
            return False
        self._fd = fd
        self._method = "lockf"
        self._write_holder()
        return True

    def _acquire_mkdir(self) -> bool:
        """Repli portable : `mkdir` est atomique sur tout POSIX.

        Utilise lorsque `lockf` n'est pas disponible. Un verrou perime
        (porteur disparu et ecriture ancienne) est repris, ce qui evite
        qu'un `kill -9` bloque indefiniment les executions suivantes.
        """
        try:
            os.mkdir(self.path)
            self._method = "mkdir"
        except FileExistsError:
            self._holder = self._read_holder()
            if self._definitively_held or not self._reclaim_if_stale():
                return False
            try:
                os.mkdir(self.path)
                self._method = "mkdir"
            except FileExistsError:  # course avec un autre processus
                return False
        except OSError as exc:
            raise OsdError(
                f"verrou impossible a creer : {self.path} ({exc.strerror})",
                ec.PREREQ,
            ) from None
        self._write_holder()
        return True

    def _reclaim_if_stale(self) -> bool:
        """Reprend un verrou `mkdir` dont le porteur n'existe plus."""
        holder = self._read_holder()
        if holder is None:
            return False
        pid = holder.get("pid")
        age = time.time() - float(holder.get("time", 0) or 0)
        if age < STALE_AFTER_S:
            return False
        if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
            return False
        try:
            import shutil

            shutil.rmtree(self.path)
        except OSError:  # pragma: no cover
            return False
        return True

    # -- Metadonnees ------------------------------------------------------
    _holder: Optional[dict] = None

    def _write_holder(self) -> None:
        import json

        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "time": time.time(),
            "description": self.description,
        }
        blob = json.dumps(payload)
        if self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.write(self._fd, blob.encode("utf-8"))
            except OSError:  # pragma: no cover
                pass
            return
        try:
            (self.path / "holder.json").write_text(blob, encoding="utf-8")
            os.chmod(self.path / "holder.json", 0o600)
        except OSError:  # pragma: no cover
            pass

    def _read_holder(self) -> Optional[dict]:
        import json

        try:
            if self._method == "mkdir" or self.path.is_dir():
                blob = (self.path / "holder.json").read_text(encoding="utf-8")
            else:
                blob = self.path.read_text(encoding="utf-8")
            data = json.loads(blob)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    # -- Liberation -------------------------------------------------------
    def release(self) -> None:
        import shutil

        if self._fd is not None:
            try:
                fcntl.lockf(self._fd, fcntl.LOCK_UN)
            except (OSError, NameError):  # pragma: no cover
                pass
            try:
                os.close(self._fd)
            except OSError:  # pragma: no cover
                pass
            self._fd = None
            try:
                self.path.unlink()
            except OSError:
                # Le fichier peut avoir deja disparu : sans consequence,
                # le verrou est de toute facon libere a la fermeture du fd.
                pass
            return
        if self._method == "mkdir":
            try:
                shutil.rmtree(self.path)
            except OSError:  # pragma: no cover
                pass

    @property
    def method(self) -> str:
        return self._method

    def holder_text(self) -> str:
        """Decrit le titulaire du verrou, pour un message d'erreur utile.

        Le but n'est pas d'identifier un processus a la machine, mais
        d'aider l'exploitant a repondre a la seule question qui compte :
        *est-ce que j'attends, ou est-ce que ce run est bloque ?*

        Il faut donc plus que `pid` et `hote`. Le `pid` seul oblige a
        ouvrir un second terminal pour `ps` ; et sans l'anciennete, un
        verrou tenu depuis six heures par un `expdp` legitime se
        confond avec un processus mort que personne n'a nettoye. La
        description contient le `run_id`, qui renvoie directement au
        rapport et au journal de l'execution en cours.
        """
        h = self._holder or {}
        if not h:
            return "inconnu"
        morceaux = [f"pid {h.get('pid', '?')} sur {h.get('host', '?')}"]
        age = _age(h.get("time"))
        if age:
            morceaux.append(age)
        description = str(h.get("description") or "").strip()
        if description:
            morceaux.append(description)
        return ", ".join(morceaux)


def _age(timestamp) -> str:
    """Anciennete d'un verrou, en language d'exploitant.

    Volontairement approximative et jamais en erreur : une horloge
    fausse, un champ absent ou un `time` de type inattendu rendent la
    fonction muette plutot que de faire echouer le message d'erreur qui
    doit, lui, toujours aboutir.
    """
    try:
        secondes = time.time() - float(timestamp)
    except (TypeError, ValueError):
        return ""
    if secondes < 90:
        return "depuis moins de 2 min"
    if secondes < 5400:
        return f"depuis {int(secondes // 60)} min"
    return f"depuis {int(secondes // 3600)} h"


def _pid_alive(pid: int) -> bool:
    """Indique si un PID existe encore sur cette machine.

    `kill(pid, 0)` ne renvoie pas d'erreur si le processus existe mais
    n'appartient pas a l'utilisateur courant : c'est exactement ce qu'on
    veut, le verrou devant etre partage entre comptes de l'equipe.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:  # pragma: no cover
        return True
    return True


def acquire(cfg, *, description: str = "") -> Lock:
    """Construit et acquiert le verrou correspondant a la configuration."""
    directory = Path(cfg.get("LOCK_DIR") or cfg.get("WORK_DIR") or "work")
    return Lock(directory / f"{lock_key(cfg)}.lock", description=description)
