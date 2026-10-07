"""Transfert du dump entre l'hote source et l'hote cible.

Le dump n'est visible que de l'hote ou tourne `expdp` : le chemin
`DIRECTORY` n'est pas atteignable depuis le serveur de saut. Trois
topologies sont donc possibles, et le choix n'est pas neutre :

* **partage** — les deux bases voient le meme systeme de fichiers : aucun
  transfert reseau, le dump reste ou `expdp` l'a ecrit.
* **relais** — les deux cotes sont distantes, et le dump passe par le
  serveur de saut. Deux formes selon le mode d'authentification, cf.
  `_staging_requis` : `scp -3` par cle, deux sauts separes par mot de
  passe — au prix d'une temporisation sur le serveur de saut, qui doit
  en disposer de la place.
* **mixte** — un cote local et un cote distant : refuse. `_topologie` le
  nomme pour le designer dans le refus, pas pour l'ignorer.

Le choix du mecanisme (`rsync`, `scp`, `sftp`, `scp` en mode heritage) est
determine par **sonde de capacite reelle**, jamais par la presence d'un
binaire. En effet, un `scp` present peut ne pas fonctionner : le
sous-systeme `sftp` peut etre absent du `sshd` de l'AIX, et seule une
copie reelle le revele. C'est le cas le plus courant sur AIX, ou le
`sshd` est ancien.

Deux modes d'authentification coexistent. **Par cle**, la commande porte
`BatchMode=yes` : une erreur d'authentification echoue au lieu de
bloquer sur une invite, sous cron, jusqu'a expiration du crontab. **Par
mot de passe** — le secret de l'inventaire, lu aupres du runner —
`BatchMode` vaut `no`, `NumberOfPasswordPrompts=1` borne la tentative, et
le secret est remis a `sshpass` par l'environnement, jamais par argument :
`ps` ne le montrerait nulle part.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

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
#:
#: `sftp` reste liste, et echoue toujours : son nom doit apparaitre dans le
#: compte rendu des tentatives plutot que disparaitre du vocabulaire. Il ne
#: peut pas transferer en `relais` -- son script ne connait qu'un hote --
#: et `_sftp_command` le dit en une phrase au lieu de le laisser echouer
#: sur un chemin qui n'existe pas. Le retirer de la liste rendrait
#: `TRANSFER_MODE=sftp` muet, alors qu'un operateur peut l'avoir pose.
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


#: Nom de la variable d'environnement par laquelle `sshpass` lit le mot
#: de passe. C'est la seule voie supportee : `sshpass -p` mettrait le
#: secret dans la ligne de commande, donc dans `ps` pour tout utilisateur
#: du serveur de saut, et la trace resterait dans les journaux du
#: superviseur.
_SSHPASS_ENV = "SSHPASS"

#: Options `sshpass` imposees quand un mot de passe est employe.
#:
#: `sshpass` n'accepte que ses propres options (`-f`, `-d`, `-p`, `-e`).
#: Les options **SSH** ne se mettent pas ici : elles s'appliquent a la
#: commande enveloppee, et `sshpass` les refuserait. L'erreur dit
#: `invalid option -- 'o'`, ce qui ne rattache pas la cause au bon
#: niveau -- on cherche une option SSH chez le mauvais programme.
#:
#: D'ou la separation : `_prefixe_sshpass` ne construit que
#: `sshpass -e`, et `_merge_opts` pose `BatchMode=no` et
#: `NumberOfPasswordPrompts=1` sur la commande reellement executee.
_SSHPASS_BIN = "sshpass"


def _env_avec_mot_de_passe(mot_de_passe: str) -> Optional[Dict[str, str]]:
    """Environnement du transfert, avec `SSHPASS` si un mot de passe est fourni.

    Retourne `None` quand aucun mot de passe n'est fourni : dans ce cas
    le transfert tente l'authentification par cle, et l'echec doit venir
    de `ssh` lui-meme, qui en dit plus qu'un `sshpass` sans secret.
    """
    if not mot_de_passe:
        return None
    env = dict(os.environ)
    env[_SSHPASS_ENV] = mot_de_passe
    return env


def _prefixe_sshpass(mot_de_passe: str) -> List[str]:
    """Prefixe `sshpass` a prependre a une commande de transfert.

    `-e` est la seule voie acceptee. `sshpass -p` mettrait le secret dans
    la ligne de commande, donc dans `ps` pour tout utilisateur du
    serveur de saut, et l'empreinte resterait dans les journaux du
    superviseur.
    """
    if not mot_de_passe:
        return []
    return [_SSHPASS_BIN, "-e"]


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
        ssh_password: str = "",
        probe_cache_dir: Optional[Path] = None,
        bandwidth_limit: str = "",
    ) -> None:
        self.source = source_runner
        self.target = target_runner
        self.mode = mode
        # Le transfert n'est **pas** execute par Ansible : ce sont
        # `scp`/`rsync`/`sftp` lances depuis le serveur de saut, donc
        # hors du chemin du runner. L'authentification par mot de passe
        # passe donc par `sshpass`, qui lit le secret dans
        # l'environnement -- jamais dans un argument, ou `ps` le
        # montrerait. Vide = pas de mot de passe fourni, et le
        # transfert echouera sur une authentification par cle absente.
        self.ssh_password = ssh_password
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
                 "chaque hote : `scp` et `rsync` y sont lances et "
                 "designent les deux hotes. Sur AIX, si le sous-systeme "
                 "sftp du `sshd` est absent, TRANSFER_MODE=scp-legacy "
                 "repond au meme besoin.",
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

        # Options **brutes** ici : chaque constructeur de commande les
        # fusionne lui-meme, et c'est lui qui sait sous quelle forme
        # OpenSSH les veut (`-e "ssh ..."` pour `rsync`, `-o` repete
        # pour `scp`). Fusionner ici ajouterait une seconde fois
        # `BatchMode` et `NumberOfPasswordPrompts` : sans effet sur le
        # comportement, mais visible dans les journaux et dans le
        # rapport, ou une repetition signe une double responsabilite.
        ssh_opts = _opts_du_runner(self.source)

        if backend == "sftp":
            ok, reason = self._sftp_command(src_dir, dst_dir, name, ssh_opts)
            if not ok:
                self._last_reason = reason
                return False
            return True

        if self._staging_requis(backend):
            # Deux invocations plutot qu'une : c'est la seule facon de
            # faire repondre `sshpass` deux fois.
            return self._run_staged(backend, src_dir, dst_dir, name, ssh_opts, probe=probe)

        if backend == "rsync":
            commandes = [self._rsync_command(src_dir, dst_dir, name, ssh_opts)]
        elif backend in ("scp", "scp-legacy"):
            commandes = [
                self._scp_command(
                    src_dir, dst_dir, name, ssh_opts, legacy=backend == "scp-legacy"
                )
            ]
        else:  # pragma: no cover - garde-fou
            self._last_reason = f"backend inconnu: {backend}"
            return False
        return self._execute(backend, commandes, probe=probe)

    def _execute(
        self, backend: str, commandes: Sequence[Sequence[str]], *, probe: bool
    ) -> bool:
        """Enveloppe `sshpass` et execution, jusqu'au premier echec.

        L'enveloppe est posee ici, une fois pour tous les backends :
        `rsync` et `scp` appellent `ssh` en sous-processus, et c'est ce
        sous-processus, lui, qui a besoin du mot de passe.

        Chaque commande est enveloppee **separement**. `sshpass` ne sait
        repondre qu'a une seule invite par invocation — la seconde est
        prise pour un mauvais mot de passe, et il termine sur un rc 5
        sans un mot de message (mesure sur `scp -3`, cf.
        `_staging_requis`). Une liste de deux commandes est donc la
        forme normale du transfert par mot de passe, et une liste d'une
        seule celle du transfert par cle.
        """
        for commande in commandes:
            argv = _prefixe_sshpass(self.ssh_password) + list(commande)
            try:
                proc = _run_command(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=_PROBE_TIMEOUT if probe else None,
                    check=False,
                    env=_env_avec_mot_de_passe(self.ssh_password),
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

    # -- Transfert en deux sauts (mot de passe) ---------------------------
    def _staging_requis(self, backend: str) -> bool:
        """Deux sauts separes, avec temporisation sur le serveur de saut.

        `scp -3` — et le `distant-a-distant` de `rsync` — ouvre **deux**
        sessions `ssh` dans un seul processus. En authentification par
        cle, les deux repondent silencieusement et une commande suffit.

        Par mot de passe, une seule des deux invitations trouve une
        reponse : `sshpass` voit une seconde invite, la prend pour la
        preuve que le premier essai a ete refuse, et termine (rc 5,
        stderr vide). C'est mesure, pas suppose — et le remede n'est pas
        de changer de client : un secret par invocation, donc deux
        invocations.

        La condition est donc le secret lui-meme. La topologie aussi :
        en `partage` il n'y a rien a copier, et `_run_backend` n'est de
        toute facon jamais atteint.
        """
        if not self.ssh_password:
            return False
        if self._topologie() != RELAIS:
            return False
        return backend in ("scp", "scp-legacy", "rsync")

    def _staging_root(self) -> Path:
        """Repertoire local ou le dump est temporise entre les deux sauts.

        `WORK_DIR/probes` designe `WORK_DIR` : ce repertoire est deja
        impose **local** (jamais NFS, cf. la configuration), cree et
        permis au debut du run. C'est donc le seul endroit du serveur de
        saut dont la place soit d'une lecture immediate.

        Sans cache — un usage isole de la classe — on retombe sur le
        repertoire temporaire du systeme : la seule autre certitude, et
        un endroit ou un depot ephemere est la norme plutot que l'exception.
        """
        if self.cache_dir is not None:
            racine = self.cache_dir.parent / "staging"
        else:
            racine = Path(tempfile.gettempdir()) / "osd-staging"
        try:
            racine.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TransferError(
                "repertoire de temporisation inutilisable sur le serveur de saut",
                detail=[f"{racine}: {exc.strerror or exc}"],
                hint="Le transfert par mot de passe fait passer le dump par "
                     "ce repertoire entre les deux sauts. En corriger les "
                     "droits, ou poser une cle SSH pour revenir au "
                     "transfert direct (scp -3).",
            )
        return racine

    @staticmethod
    def _staging_path(racine: Path, name: str) -> Path:
        """Nom de temporisation : deux runs ne peuvent pas se croiser.

        Le verrou de run empeche deux executions simultanees de l'outil,
        mais rien n'empeche un echec brutal d'en laisser un apres soi,
        ni un operateur de lancer deux instances depuis deux terminaux.
        Le pid et l'horloge au milliseconde rendent le croisement
        impossible, et le prefixe `.osd-stage-` rend le residu
        identifiable d'un simple `ls` — un dump n'a jamais ce nom.
        """
        return racine / (
            f".osd-stage-{os.getpid()}-{time.time_ns()}-{PurePosixPath(name).name}"
        )

    def _place_suffisante(self, racine: Path, src_dir: str, name: str) -> bool:
        """Le dump temporise t-il dans la place du serveur de saut ?

        Le controle est anterieur a la premiere copie. Sans lui, l'echec
        surviendrait a mi-parcours, signale par un message du systeme de
        fichiers qui ne nomme ni le fichier ni la topologie, et apres
        avoir occupe le double de la place necessaire.

        Un `OSError` de `disk_usage` ne bloque pas : mieux vaut laisser
        `scp` constater lui-meme, avec son propre message, que de
        refuser un transfert que le systeme aurait peut-etre pu accueillir.
        """
        taille = _stat_remote(self.source, src_dir, name)
        try:
            libre = shutil.disk_usage(str(racine)).free
        except OSError:  # pragma: no cover - systeme exotique
            return True
        if taille <= libre:
            return True
        self._last_reason = (
            f"place insuffisante sur le serveur de saut pour temporiser {name} "
            f"({human_bytes(taille)} requis, {human_bytes(libre)} libres "
            f"dans {racine})"
        )
        return False

    def _staged_commands(
        self,
        backend: str,
        src_dir: str,
        dst_dir: str,
        name: str,
        ssh_opts: Sequence[str],
        local: Path,
    ) -> Tuple[List[str], List[str]]:
        """Les deux commandes : descendre sur le saut, puis remonter.

        Chaque commande n'a qu'un hote distant, donc qu'une invitation,
        donc qu'une reponse de `sshpass`. Le `-3` disparait — il n'y a
        plus deux sauts a faire passer par un meme processus — et
        `local` tient la place du second hote dans les deux cas.
        """
        descendre = f"{_hote(self.source)}:{PurePosixPath(src_dir) / name}"
        monter = _cible(self.target, dst_dir, name)
        if backend == "rsync":
            argv = ["rsync", "-a", "--partial", "--timeout=120"]
            ssh = self._rsync_e(ssh_opts)
            if ssh:
                argv.extend(["-e", ssh])
            return argv + [descendre, str(local)], argv + [str(local), monter]
        legacy = backend == "scp-legacy"
        return (
            self._scp_argv(ssh_opts, legacy, [descendre, str(local)], trois=False),
            self._scp_argv(ssh_opts, legacy, [str(local), monter], trois=False),
        )

    def _run_staged(
        self, backend: str, src_dir: str, dst_dir: str, name: str,
        ssh_opts: Sequence[str], *, probe: bool,
    ) -> bool:
        """Transfert en deux sauts, avec temporisation sur le saut.

        Le fichier est retire dans un `finally` : que la seconde copie
        reussisse, echoue, ou soit interrompue, le serveur de saut n'a
        rien a conserver. Seul un SIGKILL — le second signal du rituel
        d'arret — peut en laisser un apres soi, et il est alors
        reconnaissable a son prefixe (cf. `_staging_path`).
        """
        racine = self._staging_root()
        local = self._staging_path(racine, name)
        try:
            if not self._place_suffisante(racine, src_dir, name):
                return False
            descendre, monter = self._staged_commands(
                backend, src_dir, dst_dir, name, ssh_opts, local
            )
            LOG.info(
                "transfert %s en deux sauts, temporisation %s", name, local
            )
            if not self._execute(backend, [descendre], probe=probe):
                return False
            return self._execute(backend, [monter], probe=probe)
        finally:
            try:
                local.unlink()
            except OSError:
                pass

    def _rsync_e(self, ssh_opts: Sequence[str]) -> str:
        """La valeur de `-e` pour `rsync`, ou chaine vide.

        Une seule paire `-o`/valeur par option, parce que `rsync`
        decoupe la valeur sur les espaces avant de la donner a un
        shell : sans le `-o`, `ConnectTimeout=10` deviendrait un nom
        d'hote a joindre, et l'echec parlerait de resolution de nom.
        """
        options = _merge_opts(ssh_opts, self.ssh_password)
        if not options:
            return ""
        ssh = "ssh"
        for opt in options:
            ssh += " -o " + opt
        return ssh

    def _rsync_command(
        self, src_dir: str, dst_dir: str, name: str, ssh_opts: Sequence[str]
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

        Chaque option est prefixee de son `-o`, et c'est obligatoire.
        `rsync` decoupe la valeur de `-e` sur les espaces et la passe a
        un shell, donc

            rsync -e "ssh ConnectTimeout=10 BatchMode=no"

        fait de `ConnectTimeout=10` le **nom d'hote** a joindre, et
        echoue sur « Could not resolve hostname connecttimeout=10 ».
        L'erreur ne parle que de resolution de nom, alors que la cause
        est une ligne de commande mal construite -- le genre de defaut
        qui envoie vers le DNS alors que le probleme est ici.

        Contrairement a `scp`, `rsync` fait passer un transfert
        distant-a-distant **par la machine qui l'a lance** : les deux
        sessions `ssh` sont ouvertes depuis le serveur de saut, qui a
        deja les deux cles. Aucune option n'est donc necessaire ici, et
        aucune cle n'a etre distribuee entre les hotes.
        """
        argv = ["rsync", "-a", "--partial", "--timeout=120"]
        # Aucun delai de connexion n'est ajoute ici. L'option qui le
        # portait etait `--contimeout`, honoree par `rsync` face un
        # **demon** seulement : en mode `rsync -e ssh` -- le seul que
        # nous employons -- elle est refusee, et `rsync` echouait donc
        # toujours. L'echec etait masque par le repli automatique vers
        # `scp`, ce qui le rendait invisible.
        #
        # Le delai de connexion est desormais porte par `ConnectTimeout`
        # dans les options SSH, que `_merge_opts` construit a partir de
        # l'inventaire. Une sonde contre un hote injoignable echoue donc
        # en 10 s, pas en 120.
        ssh = self._rsync_e(ssh_opts)
        if ssh:
            # `-o` devant chaque option, et non un seul `-o` suivi de
            # toutes : `rsync` scinde la valeur de `-e` sur les espaces
            # avant de la donner a un shell. Voir la note de version.
            argv.extend(["-e", ssh])
        argv.extend([
            f"{_hote(self.source)}:{PurePosixPath(src_dir) / name}",
            _cible(self.target, dst_dir, name),
        ])
        return argv

    def _scp_command(
        self, src_dir: str, dst_dir: str, name: str, ssh_opts: Sequence[str], *, legacy: bool
    ) -> List[str]:
        """Commande `scp` directe : les deux sauts dans un seul processus.

        **`-3` est obligatoire en topologie `relais`.** Sans lui, `scp`
        fait le second saut **depuis la source** : la source ouvre sa
        propre session `ssh` vers la cible, avec sa propre cle. Sur ces
        hotes, ou aucune cle n'est distribuee entre AIX, l'echec est
        « Permission denied, please try again » — un message
        d'authentification qui ne dit pas que le chemin est bon et que
        seule la cle manque. La source et la cible n'ont aucune raison de
        se faire confiance : elles n'ont meme pas de raison de se connaitre.

        `-3` fait passer les deux sauts par le serveur de saut, qui a
        deja les deux cles. Le transfert reste donc exactement aussi
        controle, et ne demande aucune cle distribuee entre les hotes.

        Cette forme n'est employee que **par cle** : deux sessions dans
        un meme processus supposent deux authentifications silencieuses.
        Par mot de passe, `_staging_requis` impose la forme en deux
        commandes de `_staged_commands`, qui partage l'assemblage avec
        `_scp_argv`.
        """
        return self._scp_argv(
            ssh_opts,
            legacy,
            [
                f"{_hote(self.source)}:{PurePosixPath(src_dir) / name}",
                _cible(self.target, dst_dir, name),
            ],
            trois=True,
        )

    def _scp_argv(
        self,
        ssh_opts: Sequence[str],
        legacy: bool,
        operandes: Sequence[str],
        *,
        trois: bool,
    ) -> List[str]:
        """Assemblage commun des formes directe et en deux sauts de `scp`.

        `-3` et `-O` sont poses **avant** les `-o` : `scp` analyse les
        options par ordre, et une option placee apres une paire
        d'arguments serait prise pour un chemin.

        Le mode heritage (`-O`) impose l'ancien protocole `scp` distant
        au lieu du SFTP de l'OpenSSH 9. C'est le mode a utiliser sur un
        AIX dont le `sshd` ne fournit pas le sous-systeme sftp, ou le
        transfert echoue avec « Subsystem request failed » sans qu'aucun
        journal ne dise pourquoi. `-O` n'existe pas avant OpenSSH 9 : sa
        presence est verifiee plutot que de laisser `scp` echouer sur une
        option inconnue, dont le message ne dit pas « option
        inexistante » mais « fichier introuvable ».
        """
        argv = ["scp"]
        if trois:
            argv.append("-3")
        if legacy:
            if not _scp_supports_legacy():
                self._last_reason = "scp local sans support du mode heritage (-O)"
                raise TransferError(
                    self._last_reason,
                    detail=["OpenSSH >= 9 requis pour le mode heritage"],
                    hint="Poser TRANSFER_MODE=scp (SFTP), ou installer un "
                         "OpenSSH 9 sur le serveur de saut.",
                )
            argv.append("-O")
        for opt in _merge_opts(ssh_opts, self.ssh_password):
            argv.extend(["-o", opt])
        argv.extend(operandes)
        return argv

    def _sftp_command(
        self, src_dir: str, dst_dir: str, name: str, ssh_opts: Sequence[str]
    ) -> "tuple[bool, str]":
        """Backend `sftp` : **toujours refuse**, et pour une raison nommee.

        Il est appele par le meme code que `rsync` et `scp`, et rend le
        meme couple `(ok, raison)` -- d'ou l'absence de toute option dans
        la signature : il n'y a plus rien a configurer.

        **`sftp` ne sait pas ecrire a distance.** A la difference de `scp`
        et `rsync`, son script ne connait qu'un seul hote : `get` y
        telecharge, et le second chemin est un chemin **local au serveur
        de saut**. Un `relais` -- dont les deux cotes sont distants, par
        definition -- n'a donc aucune forme en `sftp` : le dump atterrirait
        sur le serveur de saut, la cible ne le verrait jamais, et l'import
        echouerait trois etapes plus loin sur un fichier absent.

        Le refus est donc explicite, et anterieur a tout lancement de
        processus : laisser `sftp` echouer produirait un message de fichier
        introuvable, qui envoie l'exploitant verifier des droits alors que
        le chemin est faux par construction, et deposerait reellement le
        fichier sur le serveur de saut avant d'echouer.
        """
        return False, (
            "sftp ne peut pas ecrire sur un hote distant ; "
            f"le serveur de saut deposerait le dump dans {dst_dir}, "
            "que la cible ne voit pas"
        )

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


def _cible(runner, dst_dir: str, name: str) -> str:
    """`utilisateur@hote:/chemin` — la destination **distante** du fichier.

    La source est designee par `_hote(source)`, et rien d'autre ne pouvait
    designer la cible. La commande produite par defaut etait donc :

        scp oracle@src:/pwcdata/export/f.dmp /pwcdata/import/f.dmp

    ou le second chemin est un chemin **local au serveur de saut**. Sur une
    topologie `relais` — les deux cotes distants, ce qui est la seule
    topologie de transfert — ce repertoire n'existe generalement pas sur le
    serveur de saut. Le client echouait alors sur un message de fichier
    introuvable ou de droit refuse, qui ne parle que de la destination et
    envoie l'exploitant verifier des permissions sur le mauvais hote. La
    cause reelle — un chemin construit pour une machine qui n'est pas celle
    qui recoit le fichier — ne se lisait nulle part.

    La cible est donc designee comme la source l'est, et par la meme
    fonction : le compte et l'adresse viennent de l'inventaire, ce qui
    garantit qu'Ansible et `scp` designent le meme hote. Les deux ne
    peuvent plus diverger, donc le dump ne peut plus atterrir ailleurs que
    la ou l'import le cherchera.

    `scp` et `rsync` savent tous deux relier deux hotes distants. `sftp` ne
    le sait pas, et `_sftp_command` refuse ce cas plutot que de le laisser
    echouer.
    """
    return f"{_hote(runner)}:{PurePosixPath(dst_dir) / name}"


def _hote(runner) -> str:
    """`utilisateur@hote` pour une commande de transfert cote serveur.

    `scp`, `rsync` et `sftp` sont des clients du **serveur de saut** : ils
    designent donc l'hote distant, pas l'executant. Centralise ici parce
    que la regle a ete implementee trois fois, dont une fois avec un
    `lstrip("@")` sans effet : un `user` vide produisait `@hote` dans une
    version et `hote` dans les deux autres. Le rapport indiquait alors
    un hote qui n'existe pas.

    `transfer_host` et `transfer_user` priment sur `host` et `user` :
    ils portent ce que **ces clients** doivent joindre, et non ce que
    l'inventaire nomme. La distinction n'est pas academique : un nom
    d'inventaire n'est resoluble que par Ansible, donc `scp osaix:...`
    echouerait sur une resolution de nom alors que tout le run aurait
    reussi. Un inventaire dont le nom est directement joignable -- une
    adresse, un alias DNS -- donne la meme valeur des deux cotes, et
    rien ne change.

    L'utilisateur suit la meme regle : `ansible_user` designe le compte
    d'exploitation de l'hote, qui n'est pas forcement celui du serveur de
    saut.
    """
    host = getattr(runner, "transfer_host", None) or getattr(runner, "host", "")
    user = getattr(runner, "transfer_user", None)
    if user is None:
        user = getattr(runner, "user", "") or ""
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


def _opts_du_runner(runner) -> List[str]:
    """Options SSH du runner, qu'il les expose en attribut ou en methode.

    Le contrat a change avec le transport. `RemoteRunner` portait un
    attribut `ssh_opts`, pose a la construction depuis la configuration.
    `AnsibleRunner` lit l'inventaire a la demande, donc sa methode
    `ssh_opts` declenche une lecture du coffre -- et la valeur n'est donc
    pas connue a la construction.

    Sans cette adaptation, `getattr(runner, "ssh_opts", [])` rendrait la
    methode elle-meme, et la commande de transfert recevrait une liste
    contenant un objet lie : aucune erreur, aucun transfert possible, et
    un echec bien plus tardif a diagnostiquer. C'est le genre de defaut
    qu'un test d'egalite de chaines ne revele pas ; d'ou un test sur la
    commande rendue, qui est ce que voit reellement l'exploitant.
    """
    opts = getattr(runner, "ssh_opts", None)
    if callable(opts):
        try:
            opts = opts()
        except Exception:  # pragma: no cover - le runner rapporte lui-meme
            return []
    if isinstance(opts, str):
        return [o for o in opts.split() if o]
    if isinstance(opts, (list, tuple)):
        return [str(o) for o in opts]
    return []


def _merge_opts(ssh_opts: Sequence[str], mot_de_passe: str = "") -> List[str]:
    """Assemble les options SSH en conservant un ordre deterministe.

    Le traitement de `BatchMode` depend du mode d'authentification, et
    c'est deliberement l'inverse dans les deux cas :

    * **par cle** (pas de mot de passe fourni) — `BatchMode=yes` est ajoute
      comme toujours. Sans lui, une erreur d'authentification ouvrirait
      une invite interactive, et le run resterait bloque jusqu'a
      l'expiration du crontab. C'est la garantie d'origine.

    * **par mot de passe** — `BatchMode` est pose a `no`, parce qu'il
      interdit toute invite, donc toute saisie. Le blocage qu'il evitait
      est alors prevenu par `NumberOfPasswordPrompts=1`, pose par
      `_prefixe_sshpass` : `ssh` tente une fois, puis echoue. Le defaut
      est le meme, le remede change.
    """
    base = [o for o in ssh_opts if not o.strip().lower().startswith("batchmode")]
    # Toute variante de `BatchMode` est ecartee, pas seulement l'absence
    # du mot-cle : `BatchMode=no` designerait exactement ce que cette
    # fonction annonce empecher, en mode cle.
    if mot_de_passe:
        return base + ["BatchMode=no", "NumberOfPasswordPrompts=1"]
    return base + ["BatchMode=yes"]


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
