"""Transfert du dump entre l'hote source et l'hote cible.

Le dump n'est visible que de l'hote ou tourne `expdp` : le chemin
`DIRECTORY` n'est pas atteignable depuis le serveur de saut. Trois
topologies sont donc possibles, et le choix n'est pas neutre :

* **local** — les deux bases sont sur le meme serveur de fichiers, ou le
  dump est ecrit dans un DIRECTORY partage. Aucun transfert reseau.
* **relais** (defaut) — le dump descend sur le serveur de saut, puis
  remonte vers la cible. Simple, mais le dump transite en deux fois et le
  serveur de saut doit disposer de la place correspondante.
* **direct** — le dump va directement de l'hote source a l'hote cible, le
  serveur de saut n'etant qu'un orchestrateur. C'est la seule option
  viable quand le dump depasse la place disponible sur le saut.

Le choix du mecanisme (`rsync`, `scp`, `sftp`, `scp` en mode heritage) est
determine par **sonde de capacite reelle**, jamais par la presence d'un
binaire. En effet, un `scp` present peut ne pas fonctionner : le
sous-systeme `sftp` peut etre absent du `sshd` de l'AIX, et seule une
copie reelle le revele. C'est le cas le plus courant sur AIX, ou le
`sshd` est ancien.

Aucun mot de passe n'est utilise : le transfert repose sur une cle SSH,
avec `BatchMode=yes` pour qu'une erreur d'authentification echoue au
lieu de bloquer sur une invite.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence

from ..errors import TransferError
from ..logging_setup import get_logger
from ..redact import redact
from ..runner import build_script, load_body

LOG = get_logger()

#: Corps de script : verification d'un chemin distant (existence,
#: type, taille, place libre) en un seul aller-retour.
_PROBE_PATH_BODY = "remote_pathinfo.sh"

#: Backends essayes dans cet ordre quand TRANSFER_MODE=auto.
#:
#: `scp-legacy` est place avant `scp` volontairement : sur un AIX, c'est
#: la seule voie qui fonctionne quand le `sshd` n'expose pas le
#: sous-systeme `sftp`, ce qui est le cas le plus courant.
AUTO_ORDER = ("rsync", "scp-legacy", "scp", "sftp")

#: Duree de validite d'une sonde de capacite. Court : un `sshd` peut
#: etre reconfigure entre deux runs, et un cache d'une journee
#: conduirait a un echec d transfert inexplique.
PROBE_TTL_S = 3600

#: Topologies. Les deux noms retenus sont ceux que porte
#: `TransferOutcome.method`, donc le rapport et la decision technique
#: disent la meme chose.
#:
#: `MIXTE` n'est pas une topologie **supportee** : elle est nommee pour
#: etre refusee avec un message qui la designe, plutot que pour etre
#: traitee comme `PARTAGE`. Cf. `_topologie`.
PARTAGE = "partage"
RELAIS = "relais"
MIXTE = "mixte"

#: Nom du backend correspondant a `PARTAGE`. Ce n'est pas un mecanisme
#: de copie : c'est l'absence de copie. Le nom est malgre tout employe,
#: parce que c'est lui que porte `TransferOutcome.backend` et que
#: l'exploitant doit pouvoir le retrouver dans le rapport.
LOCAL = "local"

#: Fichier d'echantillon utilise pour la sonde reelle. Volontairement
#: minuscule : la sonde doit rester negligeable devant le transfert, tout
#: en etant un vrai aller-retour ecriture + relecture + suppression.
_PROBE_CONTENT = b"osd-probe\n" * 16

#: Unique point de sortie reseau du module : `scp`, `rsync`, `sftp`.
#:
#: Il n'est pas la qu'on a le choix d'ecrire directement — une facade
#: ajoutee pour les tests serait du bruit. Il est la **separation** qui
#: vaut : tout ce que la machine envoie sur le reseau passe par une
#: ligne nommee, donc la suite de tests sait exactement ce qu'elle
#: remplace, et unites dans la logique de selection de backend.
#:
#: `LocalRunner` et le reste du projet utilisent `subprocess.run`
#: directement ; `_scp_supports_legacy` aussi, parce que poser une
#: option invalide a un binaire local n'emprunte aucun chemin reseau.
#: Le distinguer ici evite qu'un test croie substituer le reseau la ou
#: il n'y en a pas.
_run_command = subprocess.run


@dataclass
class TransferOutcome:
    """Resultat d'un transfert."""

    backend: str
    files: List[str] = field(default_factory=list)
    bytes_total: int = 0
    duration_s: float = 0.0
    #: Topologie employee, et non detail technique. `partage` quand les
    #: deux cotes voient le meme repertoire, `relais` quand le dump
    #: transite par le serveur de saut.
    #:
    #: Le champ portait precedemment la premiere option SSH en mode
    #: distant, et `local` en mode partage : deux natures sous le meme
    #: nom, dans le meme rapport. Rien ne.distinguait « le dump a transite
    #: par le serveur de saut » de « l'option ConnectTimeout vaut 30 ».
    method: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "backend": self.backend,
            "method": self.method,
            "files": list(self.files),
            "bytes_total": self.bytes_total,
            "duration_s": round(self.duration_s, 3),
        }


class TransferBackend:
    """Selection et execution du mecanisme de transfert.

    La sonde est le coeur de cette classe. Un binaire present ne prouve
    rien ; seul un aller-retour reel prouve la capacite. Chaque tentative
    est tracee, ce qui rend l'echec diagnosticable : « scp a echoue »
    n'aide personne, « scp a echoue car le sous-systeme sftp est absent,
    scp-legacy a reussi » aide.
    """

    def __init__(
        self,
        *,
        source_runner,
        target_runner,
        mode: str = "auto",
        ssh_key: str = "",
        probe_cache_dir: Optional[Path] = None,
        bandwidth_limit: str = "",
    ) -> None:
        self.source = source_runner
        self.target = target_runner
        self.mode = mode
        self.ssh_key = ssh_key
        self.cache_dir = Path(probe_cache_dir) if probe_cache_dir else None
        self.bandwidth_limit = bandwidth_limit
        self._resolved: Optional[str] = None

    # -- Sonde de capacite ------------------------------------------------
    def resolve(self, *, src_dir: str = "", dst_dir: str = "") -> str:
        """Retourne le backend effectivement utilise.

        En mode `auto`, les backends sont essayes dans l'ordre. Le premier
        qui passe la sonde gagne. En mode force, une seule tentative est
        faite et l'echec est remonte tel quel : l'exploitant a pose un
        choix, il doit apprendre qu'il ne tient pas.

        Les repertoires reels sont passes a la sonde : la capacite mesuree
        doit etre celle du transfert a venir, pas celle d'un `DIRECTORY`
        temporaire.
        """
        if self._resolved:
            return self._resolved

        topologie = self._topologie()
        if topologie == PARTAGE:
            self._resolved = LOCAL
            return self._resolved
        if topologie == MIXTE:
            # Atteignable seulement si la validation de configuration a ete
            # contournee. On refuse ici plutot que de pretendre que le dump
            # est deja visible de l'autre cote : un « aucun transfert »
            # affiche ici produirait un import qui echoue trois etapes plus
            # loin, sans lien visible avec la cause.
            raise TransferError(
                "topologie mixte refusee : un cote local et un cote distant",
                detail=[
                    f"source : {self._cote(self.source)}",
                    f"cible  : {self._cote(self.target)}",
                ],
                hint="Soit heberger les deux bases sur le serveur de saut "
                     "(les deux *_HOST vides), soit les designer toutes "
                     "les deux par *_HOST.",
            )

        # `TRANSFER_MODE` est un enum en majuscules (cf. config.SCHEMA),
        # les noms de backends sont en minuscules : la conversion est faite
        # ici, une fois, plutot que dans chaque comparaison.
        mode = (self.mode or "auto").lower()

        if mode == LOCAL:
            self._resolved = LOCAL
            return self._resolved

        candidates: Sequence[str]
        if mode in AUTO_ORDER:
            # Mode force : une seule tentative, et l'echec remonte tel
            # quel. C'est le comportement attendu d'un choix explicite.
            candidates = (mode,)
        else:
            candidates = AUTO_ORDER

        failures: List[str] = []
        for backend in candidates:
            if self._probe_cached(backend):
                LOG.info("backend de transfert %s (sonde en cache)", backend)
                self._resolved = backend
                return backend
            ok, reason = self._probe(backend, src_dir, dst_dir)
            if ok:
                LOG.info("backend de transfert retenu : %s", backend)
                self._cache_probe(backend)
                self._resolved = backend
                return backend
            LOG.info("backend %s ecarte : %s", backend, reason)
            failures.append(f"{backend}: {reason}")

        raise TransferError(
            "aucun mecanisme de transfert disponible entre les deux hotes",
            detail=failures,
            hint="Verifier l'acces SSH par cle entre le serveur de saut et "
                 "chaque hote. Sur AIX, si le sous-systeme sftp est absent, "
                 "utiliser TRANSFER_MODE=scp-legacy.",
        )

    def _topologie(self) -> str:
        """`partage`, `relais` ou `mixte` — les trois formes possibles.

        Les valeurs sont celles que porte `TransferOutcome.method`, donc
        le rapport et la decision technique ne peuvent pas diverger. La
        distinction `mixte` est explicite plutot que traitee comme
        `partage` : c'est la seule des trois dont le transfert n'a pas de
        forme connue.
        """
        source = getattr(self.source, "kind", "") == "remote"
        cible = getattr(self.target, "kind", "") == "remote"
        if source and cible:
            return RELAIS
        if not source and not cible:
            return PARTAGE
        return MIXTE

    @staticmethod
    def _cote(runner) -> str:
        """Designation d'un cote, lisible dans le message de refus.

        D'apres le `kind` et non d'apres la presence d'un nom d'hote : un
        `LocalRunner` n'a pas d'hote, donc le message doit dire « local »
        explicitement. C'est ce que l'exploitant doit corriger.
        """
        label = getattr(runner, "label", "?")
        if getattr(runner, "kind", "") == "remote":
            return f"{label} {getattr(runner, 'host', '')}".strip()
        return f"{label} local (execution sur le serveur de saut)"

    def _probe_cache_key(self, backend: str) -> Path:
        assert self.cache_dir is not None
        hosts = f"{getattr(self.source, 'host', '?')}-{getattr(self.target, 'host', '?')}"
        safe = re.sub(r"[^A-Za-z0-9.-]", "_", f"{hosts}-{backend}")
        return self.cache_dir / f"probe-{safe}.ok"

    def _probe_cached(self, backend: str) -> bool:
        if self.cache_dir is None:
            return False
        path = self._probe_cache_key(backend)
        try:
            if not path.is_file():
                return False
            age = time.time() - path.stat().st_mtime
            return age < PROBE_TTL_S
        except OSError:  # pragma: no cover
            return False

    def _cache_probe(self, backend: str) -> None:
        if self.cache_dir is None:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._probe_cache_key(backend)
            path.write_text(backend, encoding="utf-8")
            path.chmod(0o600)
        except OSError:  # pragma: no cover - cache non critique
            pass

    def _probe(
        self, backend: str, src_dir: str = "", dst_dir: str = ""
    ) -> "tuple[bool, str]":
        """Execute une sonde reelle d'aller-retour pour un backend.

        Le protocole est identique pour tous les backends : ecrire un petit
        fichier sur l'hote source, le transfers, verifier qu'il arrive
        intact cote cible, puis nettoyer les deux cotes. L'echec est donc
        attribuable au mecanisme et non a l'environnement general.

        **La sonde utilise les repertoires reels du transfert**, et non
        `/tmp`. C'est la seule facon de valider ce qui compte
        effectivement : un `DIRECTORY` Oracle est souvent monte avec des
        permissions ou un proprietaire differents de `/tmp`, et une sonde
        qui reussit la ou le vrai transfert echouerait produirait un
        diagnostic faux — le pire des cas, puisque l'exploitant
        reconfigurerait alors un mecanisme qui fonctionnait.
        """
        stamp = str(int(time.time() * 1000))
        name = f".osd-probe-{stamp}"
        src_dir = src_dir or self.source.probe_dir
        dst_dir = dst_dir or self.target.probe_dir

        # Etape 1 : le binaire doit exister des deux cotes.
        missing = self._missing_binaries(backend)
        if missing:
            return False, f"binaire absent ({', '.join(missing)})"

        # Etape 2 : aller-retour reel.
        try:
            written = self._write_probe(self.source, src_dir, name)
            if not written:
                return False, "ecriture du temoin impossible sur l'hote source"
            moved = self._run_backend(backend, src_dir, dst_dir, name, probe=True)
            if not moved:
                return False, self._last_reason or "copie refusee"
            verified = self._verify_probe(self.target, dst_dir, name)
            if not verified:
                return False, "fichier absent ou altere apres transfert"
        except TransferError as exc:
            return False, exc.message
        finally:
            self._cleanup_probe(self.source, src_dir, name)
            self._cleanup_probe(self.target, dst_dir, name)

        return True, "aller-retour verifie"

    def _missing_binaries(self, backend: str) -> List[str]:
        """Verifie la presence des binaires des deux cotes.

        Un seul aller-retour par binaire suffit : la question posee est
        « est-il installe », pas « fonctionne-t-il » — la capacite reelle
        est verifiee ensuite par la sonde d'ecriture.
        """
        missing: List[str] = []
        needs_local = backend in ("rsync", "scp", "scp-legacy", "sftp")
        if needs_local and not _which(backend.split("-")[0]):
            missing.append(f"client {backend.split('-')[0]} sur le serveur de saut")
        for side, label in ((self.source, "source"), (self.target, "cible")):
            if getattr(side, "kind", "") != "remote":
                continue
            if backend == "rsync" and not side.has_binary("rsync"):
                missing.append(f"rsync sur l'hote {label}")
        return missing

    def _write_probe(self, runner, directory: str, name: str) -> bool:
        body = load_body(_PROBE_PATH_BODY)
        script = build_script(body, ["write", directory, name], bootstrap=_probe_content_arg())
        try:
            # `mutating=True` : la sonde ecrit un fichier, elle doit donc
            # etre retenue en dry-run. Son resultat n'est pas un succes
            # simule mais un echec, ce qui est le comportement honnete :
            # sans sonde, aucun mecanisme ne peut etre retenu, et le
            # rapport doit le dire plutot que d'inventer un backend.
            res = runner.run_script(script, timeout=60, mutating=True)
        except Exception as exc:  # noqa: BLE001 - sonde : tout ecrasable
            LOG.debug("sonde: ecriture impossible sur %s: %s", runner.label, exc)
            return False
        return res.rc == 0

    def _verify_probe(self, runner, directory: str, name: str) -> bool:
        body = load_body(_PROBE_PATH_BODY)
        script = build_script(body, ["verify", directory, name])
        try:
            res = runner.run_script(script, timeout=60)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("sonde: verification impossible sur %s: %s", runner.label, exc)
            return False
        return res.rc == 0

    def _cleanup_probe(self, runner, directory: str, name: str) -> None:
        body = load_body(_PROBE_PATH_BODY)
        try:
            runner.run_script(build_script(body, ["remove", directory, name]), timeout=30)
        except Exception:  # noqa: BLE001 - nettoyage best-effort
            pass

    _last_reason: str = ""

    def _run_backend(
        self, backend: str, src_dir: str, dst_dir: str, name: str, *, probe: bool
    ) -> bool:
        """Applique le backend pour un fichier (sonde ou transfert reel).

        Le transfert est la seule operation du projet qui ne passe pas
        par le `Runner` : `scp` et `rsync` sont des clients du **serveur
        de saut**, et leur ligne de commande ne peut donc pas etre un
        script distant. Le dry-run doit malgre tout la retenir, sans quoi
        `--dry-run` transfererait des giga-octets — ce qui le rendrait
        faux, et pas seulement lent.

        La retenue est donc un controle de capacite (`allows_mutation`)
        et non un `if dry_run` : le `NullRunner` la refuse, un runner
        reel l'accorde. Un seul point de decision, dans le runner, comme
        partout ailleurs.
        """
        if not self.source.allows_mutation():
            # Le motif est pose, et non seulement journalise : sans lui,
            # l'appelant rapporterait « copie refusee », ce qui oriente
            # vers un probleme de droits alors que la copie n'a ete
            # tentee par personne. La distinction « je n'ai pas essaye »
            # et « ca n'a pas marche » est la meme que pour tout le
            # projet : le dry-run doit se lire comme tel, pas comme un
            # echec.
            self._last_reason = "transfert simule (dry-run) : aucune copie tentee"
            LOG.info("transfert simule : %s (%s)", name, backend)
            return False

        ssh_opts = _merge_opts(getattr(self.source, "ssh_opts", []), self.ssh_key)

        if backend == "rsync":
            cmd = self._rsync_command(src_dir, dst_dir, name, ssh_opts, probe)
        elif backend in ("scp", "scp-legacy"):
            cmd = self._scp_command(src_dir, dst_dir, name, ssh_opts, legacy=backend == "scp-legacy")
        elif backend == "sftp":
            ok, reason = self._sftp_command(src_dir, dst_dir, name, ssh_opts)
            if not ok:
                self._last_reason = reason
                return False
            return True
        else:  # pragma: no cover - garde-fou
            self._last_reason = f"backend inconnu: {backend}"
            return False

        try:
            proc = _run_command(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_PROBE_TIMEOUT if probe else None,
                check=False,
            )
        except FileNotFoundError:
            self._last_reason = f"client {backend} absent du serveur de saut"
            return False
        except subprocess.TimeoutExpired:
            self._last_reason = "delai depasse"
            return False
        except OSError as exc:  # pragma: no cover
            self._last_reason = exc.strerror or "erreur systeme"
            return False

        if proc.returncode != 0:
            self._last_reason = _classify_error(proc.stderr)
            return False
        return True

    def _rsync_command(
        self, src_dir: str, dst_dir: str, name: str, ssh_opts: Sequence[str], probe: bool
    ) -> List[str]:
        """Commande `rsync` : le plus robuste, et le seul resumable.

        `-a` conserve les attributs, `--partial` permet de reprendre un
        transfert interrompu plutot que de tout refaire, ce qui compte
        pour un dump de plusieurs giga-octets.

        **Toutes** les options SSH tiennent dans un seul `-e`. Les
        emettre une par une donneait :

            rsync -e "ssh opt1" -e "ssh opt2" ...

        et `-e` étant a valeur unique, seule la derniere comptait. Voir
        la note de version sur ce defaut : c'est le genre d'erreur qui
        ne se manifeste qu'en cron, donc en production.
        """
        argv = ["rsync", "-a", "--partial", "--timeout=120"]
        if probe:
            argv.append("--contimeout=30")
        options = _merge_opts(ssh_opts, self.ssh_key)
        if options:
            argv.extend(["-e", "ssh " + " ".join(options)])
        argv.extend([
            f"{_hote(self.source)}:{PurePosixPath(src_dir) / name}",
            f"{PurePosixPath(dst_dir) / name}",
        ])
        return argv

    def _scp_command(
        self, src_dir: str, dst_dir: str, name: str, ssh_opts: Sequence[str], *, legacy: bool
    ) -> List[str]:
        """Commande `scp`.

        Le mode heritage (`-O`) impose l'ancien protocole `scp` distant
        au lieu du SFTP de l'OpenSSH 9. C'est le mode a utiliser sur un
        AIX dont le `sshd` ne fournit pas le sous-systeme sftp, ou le
        transfert echoue avec « Subsystem request failed » sans qu'aucun
        journal ne dise pourquoi.
        """
        argv = ["scp"]
        if legacy:
            # `-O` n'existe pas avant OpenSSH 9 : sa presence est
            # verifiee plutot que de laisser `scp` echouer sur une
            # option inconnue, dont le message ne dit pas « option
            # inexistante » mais « fichier introuvable ».
            if not _scp_supports_legacy():
                self._last_reason = "scp local sans support du mode heritage (-O)"
                raise TransferError(
                    self._last_reason,
                    detail=["OpenSSH >= 9 requis pour le mode heritage"],
                    hint="Poser TRANSFER_MODE=scp (SFTP), ou installer un "
                         "OpenSSH 9 sur le serveur de saut.",
                )
            argv.append("-O")
        for opt in _merge_opts(ssh_opts, self.ssh_key):
            argv.extend(["-o", opt])
        argv.extend([
            f"{_hote(self.source)}:{PurePosixPath(src_dir) / name}",
            f"{PurePosixPath(dst_dir) / name}",
        ])
        return argv

    def _sftp_command(
        self, src_dir: str, dst_dir: str, name: str, ssh_opts: Sequence[str]
    ) -> "tuple[bool, str]":
        """Transfert par `sftp` en mode batch, via un script sur stdin.

        Le mode batch est obligatoire : sans lui, `sftp` ouvre une invite
        interactive qui bloquerait indefiniment sous cron, sans journal
        et sans code de sortie.
        """
        batch = (
            f"get {PurePosixPath(src_dir) / name} "
            f"{PurePosixPath(dst_dir) / name}\nquit\n"
        )
        argv = ["sftp", "-b", "-", "-o", "BatchMode=yes"]
        if self.ssh_key:
            argv.extend(["-i", self.ssh_key])
        for opt in _merge_opts(ssh_opts, self.ssh_key):
            if "BatchMode" not in opt:
                argv.extend(["-o", opt])
        argv.append(_hote(self.source))
        try:
            proc = _run_command(
                argv,
                input=batch.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_PROBE_TIMEOUT,
                check=False,
            )
        except FileNotFoundError:
            return False, "client sftp absent du serveur de saut"
        except subprocess.TimeoutExpired:
            return False, "delai de connexion sftp depasse"
        if proc.returncode != 0:
            return False, _classify_error(proc.stderr)
        return True, ""

    # -- Transfert reel ---------------------------------------------------
    def run(
        self,
        *,
        src_dir: str,
        dst_dir: str,
        names: Sequence[str],
        job_name: str,
    ) -> TransferOutcome:
        """Transfere les parties du dump et rend le compte rendu.

        En topologie `partage`, les deux cotes voient le meme repertoire :
        rien n'est donc copie, et le compte rendu porte `method=partage`.
        Ce cas est traite **ici** plutot que par l'appelant, parce que
        `resolve()` est le seul qui connait la topologie — lire le mode
        configure ne suffisait pas. `TRANSFER_MODE` absent ne dit rien de
        la topologie, et le dump etant deja visible des deux cotes,
        l'etape 13 echouait sur « backend inconnu: local » en cherchant
        un mecanisme de copie pour un fichier qui n'avait pas besoin
        d'etre copie.
        """
        backend = self.resolve(src_dir=src_dir, dst_dir=dst_dir)
        started = time.monotonic()
        if backend == LOCAL:
            return self._outcome_partage(src_dir, names, started)
        total = 0
        transferred: List[str] = []

        for name in names:
            LOG.info("transfert %s (%s, backend %s)", name, human_bytes(_stat_remote(self.source, src_dir, name)), backend)
            ok = self._run_backend(backend, src_dir, dst_dir, name, probe=False)
            if not ok:
                raise TransferError(
                    f"transfert de {name} vers l'hote cible impossible",
                    detail=[self._last_reason or "raison inconnue"],
                    hint=f"Backend: {backend}. Verifier la place sur la "
                         f"cible et l'acces SSH dans les deux sens.",
                )
            size = _stat_remote(self.target, dst_dir, name)
            if size <= 0:
                raise TransferError(
                    f"{name} est absent ou vide apres transfert",
                    detail=[f"backend {backend}"],
                    hint="Le transfert a retourne un succes mais le fichier "
                         "n'a pas ete materialise : espace insuffisant cote "
                         "cible, ou droits d'ecriture sur le repertoire.",
                )
            total += size
            transferred.append(name)
            LOG.info("  %s -> %s", name, human_bytes(size))

        return TransferOutcome(
            backend=backend,
            files=transferred,
            bytes_total=total,
            duration_s=time.monotonic() - started,
            method=RELAIS,
        )

    def _outcome_partage(
        self, src_dir: str, names: Sequence[str], started: float
    ) -> TransferOutcome:
        """Compte rendu d'un transfert qui n'a rien a transfers.

        Les parties sont **mesurees** malgre tout. Le rapport doit
        annoncer le volume que l'export a produit, y compris lorsque la
        duplication s'est faite sans le deplacer : c'est ce volume qui
        est compare a la place disponible, a l'etape 9. Un compte rendu
        a zero y ferait dire « rien a transporte » alors que le dump
        existe et occupe le repertoire partage.
        """
        total = 0
        for name in names:
            total += _stat_remote(self.source, src_dir, name)
        LOG.info(
            "repertoire partage : %d partie(s) deja en place, aucun transfert",
            len(names),
        )
        return TransferOutcome(
            backend=LOCAL,
            files=list(names),
            bytes_total=total,
            duration_s=time.monotonic() - started,
            method=PARTAGE,
        )


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

_PROBE_TIMEOUT = 60


def _probe_content_arg() -> str:
    """Amorcage definissant le contenu du temoin de sonde.

    Injecte par `build_script` : le contenu est libre, il est donc pose
    dans une variable entre simples quotes, comme le parfile Data Pump.
    """
    return "\n".join([
        "osd_probecontent=" + _sq(_PROBE_CONTENT.decode("ascii")),
    ])


def _sq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _hote(runner) -> str:
    """`utilisateur@hote` pour une commande de transfert cote serveur.

    `scp`, `rsync` et `sftp` sont des clients du **serveur de saut** : ils
    designent donc l'hote distant, pas l'executant. Centralise ici parce
    que la regle a ete implementee trois fois, dont une fois avec un
    `lstrip("@")` sans effet : un `user` vide produisait `@hote` dans une
    version et `hote` dans les deux autres. Le rapport indiquait alors
    un hote qui n'existe pas.
    """
    user = getattr(runner, "user", "") or ""
    host = getattr(runner, "host", "")
    return f"{user}@{host}" if user else host


def _which(binary: str) -> Optional[str]:
    from shutil import which

    return which(binary)


def _scp_supports_legacy() -> bool:
    """Detecte si le `scp` local accepte l'option `-O`.

    L'option n'existe qu'a partir d'OpenSSH 9.0. Le premier argument
    inexistant fait afficher l'usage sur stdout, ce qui evite de dependre
    de `scp -h` (dont le code de sortie est conventionnellement 1).
    """
    try:
        proc = subprocess.run(
            ["scp", "-Z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):  # pragma: no cover
        return False
    usage = proc.stdout.decode("utf-8", "replace")
    # `-O` apparait dans l'usage sous la forme « -O  use the legacy... »
    return re.search(r"(?m)^\s*-O\b", usage) is not None


def _merge_opts(ssh_opts: Sequence[str], ssh_key: str) -> List[str]:
    """Assemble les options SSH en conservant un ordre deterministe.

    `BatchMode=yes` est toujours ajoute : sans lui, une erreur
    d'authentification provoquerait une invite interactive, et le run
    resterait bloque jusqu'a l'expiration du crontab.
    """
    # Toute variante de `BatchMode` est ecartee, pas seulement l'absence
    # du mot-cle : voir la note de version. `BatchMode=no`_referait`
    # exactement ce que cette fonction annonce empecher.
    return [
        o for o in ssh_opts if not o.strip().lower().startswith("batchmode")
    ] + ["BatchMode=yes"]


def _classify_error(stderr: bytes) -> str:
    """Transforme une erreur de transfert en diagnostic exploitable.

    Les messages de `scp`/`sftp` sont en anglais et variables selon la
    version. On ne les compare donc pas mot a mot : on extrait le motif
    constant, celui qui determine le remede.
    """
    text = redact(stderr.decode("utf-8", "replace"))
    low = text.lower()
    patterns = [
        ("no space left", "espace disque insuffisant sur l'hote"),
        ("permission denied", "permission refusee (droits ou owner du fichier)"),
        ("no such file or directory", "fichier source absent sur l'hote source"),
        ("subsystem request failed", "sous-systeme sftp absent du sshd distant (utiliser scp-legacy)"),
        ("host key verification failed", "cle d'hote non acceptee (verifier known_hosts)"),
        ("connection refused", "connexion SSH refusee"),
        ("connection timed out", "delai de connexion depasse"),
        ("could not resolve hostname", "nom d'hote non resolu"),
        ("protocol error", "incompatible de version scp/sftp"),
        ("invalid packet length", "protocole refuse par le sshd distant (essayer scp-legacy)"),
    ]
    for needle, reason in patterns:
        if needle in low:
            return f"{reason} (message d'origine: {text.strip()[:200]})"
    return text.strip()[:300] or "echec sans message exploitable"


def _stat_remote(runner, directory: str, name: str) -> int:
    """Taille d'un fichier distant, 0 s'il est absent.

    Le resultat est porte par le bloc de resultat (`OSD_SIZE`), donc
    l'analyse reste insensible a la locale de l'hote.
    """
    try:
        res = runner.run_script(
            build_script(load_body(_PROBE_PATH_BODY), ["size", directory, name]),
            timeout=60,
        )
    except Exception:  # noqa: BLE001
        return 0
    if res.rc != 0:
        return 0
    return res.get_int("OSD_SIZE", 0)


def human_bytes(n: int) -> str:
    """Formate une taille en unites lisibles, sans dependre de la locale.

    Le formatage se fait a la main plutot que par `%` de la locale, pour
    que le rapport soit identique sur tous les hotes.
    """
    if n < 1024:
        return f"{n} o"
    value = float(n)
    for unit in ("Ko", "Mo", "Go", "To"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} Po"
