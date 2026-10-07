"""Tests de la couche de transfert.

Le transfert est le seul endroit du projet ou une commande est lancee
**en local** sur le serveur de saut : `scp`, `rsync` et `sftp` sont des
clients du reseau, pas des scripts distants. Cette singularite a trois
consequences que les tests doivent verrouiller :

* la retenue en dry-run passe par `allows_mutation()` et non par un
  `if dry_run` — sinon `--dry-run` transfererait des giga-octets ;
* les options SSH doivent atteindre **toutes** la commande, y compris
  `BatchMode=yes` : c'est ce qui fait echouer l'authentification au lieu
  de bloquer sur une invite, sous cron, jusqu'a expiration du crontab ;
* la selection du mecanisme repose sur une **sonde reelle**, jamais sur
  la presence d'un binaire.

Aucun test ne touche le reseau. Le seul point de sortie reseau du
module est `transfer._run_command` (cf. la note de version de cette
constante) ; il est remplace par une copie entre deuxAeroportiers
simules, ce qui exerce toute la logique de selection sans dependre
d'un `sshd`. La substitution est **locale** a la facade : ni
`subprocess.run` ni les autres composants du projet ne sont touches, et
un test ne peut donc pas, par megarde, etrangler la copie d'un
`LocalRunner`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from typing import Any, Dict, List, Optional, Sequence, Tuple

import support  # noqa: F401

from osd.adapters import transfer as tr
from osd.adapters.null import NullRunner
from osd.adapters.transfer import (
    AUTO_ORDER,
    PROBE_TTL_S,
    TransferBackend,
    TransferOutcome,
    _classify_error,
    _hote,
    _merge_opts,
    human_bytes,
)
from osd.errors import TransferError
from osd.runner import LocalRunner, Result

#: Contenu du temoin de sonde, aligne sur `transfer._PROBE_CONTENT`.
_CONTENU_PROBE = "osd-probe\n" * 16


class RunnerFaux:
    """Runner simule qui repond comme un hote, sans reseau.

    Il repond au meme contrat que `LocalRunner` et `RemoteRunner` pour ce
    que `TransferBackend` en utilise : `kind`, `has_binary`,
    `run_script`, `allows_mutation`, `label`, `host`, `user`,
    `ssh_opts`, `probe_dir`. `kind` vaut `remote` par defaut parce que
    c'est le seul cas qui declenche une sonde : avec `local`, tout est
    court-circuite et les tests passeraient sans avoir exerce le code
    qu'ils visent.
    """

    def __init__(
        self,
        *,
        label: str = "hote",
        kind: str = "remote",
        host: str = "hote.exemple",
        user: str = "",
        binaries: Sequence[str] = ("scp", "rsync", "sftp"),
        allows_mutation: bool = True,
        probe_dir: str = "/donnees/export",
    ) -> None:
        self.label = label
        self.kind = kind
        self.host = host
        self.user = user
        self.host_binaries = set(binaries)
        self.ssh_opts: List[str] = ["ConnectTimeout=30"]
        self.probe_dir = probe_dir
        self._allows_mutation = allows_mutation
        #: Fichiers « presents » sur cet hote simule.
        self.fichiers: Dict[str, bytes] = {}
        #: Operations recues, dans l'ordre : (action, chemin).
        self.operations: List[Tuple[str, str]] = []
        #: Action a echouer, et le code de retour a simuler.
        self.echec: Optional[Tuple[str, int]] = None

    # -- Contrat du runner ------------------------------------------------

    def has_binary(self, name: str) -> bool:
        return name in self.host_binaries

    def allows_mutation(self) -> bool:
        return self._allows_mutation

    def run_script(
        self, script: str, *, timeout: Optional[int] = None, mutating: bool = False
    ) -> Result:
        """Execute le script... en interpretant ce qu'il demande.

        Une interpretation complete serait disproportionnee. On se limite
        aux quatre operations que la sonde utilise — ecrire, verifier,
        supprimer, mesurer — ce qui exerce toute la logique de
        `TransferBackend`. Le script lui-meme reste verifie par
        `test_runner.py`, qui s'execute pour de vrai.
        """
        action, chemin = _requete(script)
        self.operations.append((action, chemin))
        if self.echec and self.echec[0] == action:
            return Result(rc=self.echec[1], kv={}, stderr="echec simule")
        if action == "write":
            self.fichiers[_cle(chemin)] = _CONTENU_PROBE.encode("ascii")
            return Result(rc=0, kv={"OSD_PATH": chemin})
        if action == "verify":
            if self.fichiers.get(_cle(chemin)) == _CONTENU_PROBE.encode("ascii"):
                return Result(rc=0, kv={})
            return Result(rc=1, kv={}, stderr="absent ou altere")
        if action == "remove":
            self.fichiers.pop(_cle(chemin), None)
            return Result(rc=0, kv={})
        if action == "size":
            donnees = self.fichiers.get(_cle(chemin), b"")
            return Result(rc=0, kv={"OSD_SIZE": str(len(donnees))})
        return Result(rc=0, kv={})

    def chemins(self) -> List[str]:
        return sorted(self.fichiers)


def _requete(script: str) -> Tuple[str, str]:
    """L'action et le chemin demandes par un script du protocole.

    Les arguments sont poses en `osd_argN` a la suite de simples quotes,
    sans echappement : une valeur relue telle quelle est donc exactement
    ce qui a ete passe. C'est ce que le test observe, et c'est
    verifiable sans executer quoi que ce soit.
    """
    action = ""
    arguments: Dict[str, str] = {}
    for ligne in script.splitlines():
        if ligne.startswith("osd_arg") and "=" in ligne:
            cle, valeur = ligne.split("=", 1)
            arguments[cle.strip()] = valeur.strip().strip("'")
    if "osd_arg1" in arguments:
        action = arguments["osd_arg1"]
    chemin = arguments.get("osd_arg3", "")
    if action in ("write", "verify", "remove", "size"):
        return action, f"{arguments.get('osd_arg2', '')}/{arguments.get('osd_arg3', '')}"
    return action, chemin


def _copie_simulee(
    source: RunnerFaux,
    cible: "RunnerFaux | Sequence[RunnerFaux]",
    echecs: Sequence[str] = (),
    silencieux: bool = False,
    local: Optional[Dict[str, bytes]] = None,
):
    """Fausse commande de transfert, qui copie entre deuxAeroportiers.

    Trois comportements, parce que les trois sont distincts et que les
    confondre fait passer des tests qui ne verifient rien :

    * `echecs` — la commande **echoue** (sous-systeme sftp absent,
      fichier introuvable). Teste le chemin d'echec du client ;
    * `silencieux` — la commande repond 0 **sans deposer de fichier**.
      C'est le cas le plus trompeur : un systeme de fichiers plein peut
      etre signale par un code 0 selon la version, et la seule
      protection est la verification de presence apres copie ;
    * defaut — la copie deplace reellement le fichier, si et seulement
      si la source le detient.

    Une extremite **sans `:`** est le depot local du serveur de saut :
    c'est le temporiseur du transfert en deux sauts. Son contenu est
    note dans `local` (consultable par le test) **et** ecrit sur disque,
    parce que le code reel supprime ce fichier dans un `finally` et
    qu'un depot purement memoire rendrait cette suppression invisible.
    """
    if local is None:
        local = {}
    cibles = [cible] if isinstance(cible, RunnerFaux) else list(cible)

    def _run(cmd: Sequence[str], **kw: Any) -> subprocess.CompletedProcess:
        if any(e in cmd for e in echecs):
            return subprocess.CompletedProcess(
                cmd, 1, b"", b"scp: subsystem request failed"
            )
        # Les deux dernieres valeurs positionnelles sont la source et la
        # cible. Les isoler par position plutot que par « contient un
        # `:` » : les options `-o ConnectTimeout=10` ne commencent pas par
        # `-` une fois le decoupe fait, donc un filtre sur le separateur
        # selectionnerait des options comme si elles etaient des chemins.
        operandes = [a for a in cmd if not a.startswith("-")]
        if len(operandes) < 2:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        spec_source, chemin_cible = operandes[-2], operandes[-1]
        # `rpartition` sur le **dernier** `:` : `src.exemple:/d/f.dmp`.
        # Un `split(":", 1)[-1]` aurait rendu `src.exemple` — le nom
        # d'hote — et le registre source n'aurait jamais trouve le
        # fichier, pour une raison qui n'a rien a voir avec ce qu'on teste.
        if ":" in spec_source:
            _hote_partie, _, chemin_source = spec_source.rpartition(":")
            donnees = source.fichiers.get(_cle(chemin_source))
        else:
            # Extremite locale : registre d'abord, disque ensuite, pour
            # que le depot fonctionne meme apres rechargement du test.
            donnees = local.get(_cle(spec_source))
            if donnees is None:
                fichier = Path(_cle(spec_source))
                if fichier.is_file():
                    donnees = fichier.read_bytes()
        if donnees is None:
            return subprocess.CompletedProcess(
                cmd, 1, b"", b"scp: No such file or directory"
            )
        if silencieux:
            return subprocess.CompletedProcess(cmd, 0, b"copie silencieuse", b"")
        if ":" not in chemin_cible:
            # Depot local : ni le registre source ni celui de la cible
            # n'a de mot a dire sur un fichier qui n'est encore le leur.
            local[_cle(chemin_cible)] = donnees
            try:
                cible_locale = Path(_cle(chemin_cible))
                cible_locale.parent.mkdir(parents=True, exist_ok=True)
                cible_locale.write_bytes(donnees)
            except OSError:  # chemin refuse : le test ne tient pas au disque
                pass
            return subprocess.CompletedProcess(cmd, 0, b"copie reussie", b"")
        # La destination porte un nom d'hote : `tgt.exemple:/dst/f.dmp`.
        # Il faut donc l'**isoler** avant d'extraire le chemin, sinon la
        # cle du registre serait `/tgt.exemple/dst/f.dmp` et aucun aiguillage
        # ne serait plus possible. Le depot doit finir sur l'hote nomme,
        # pas sur « celui qui correspond au chemin » — un chemin
        # `/pwcdata/backup/export` identique des deux cotes designait le
        # mauvais hote, et la verification echouait pour une raison sans
        # rapport avec ce que le test mesure.
        _hote_cible, _, chemin_relatif = chemin_cible.rpartition(":")
        chemin_cible = chemin_relatif or chemin_cible
        if len(cibles) > 1:
            correspondants = [
                c for c in cibles
                if _cle(chemin_cible).startswith(_cle(c.probe_dir) + "/")
            ]
            if len(correspondants) != 1:
                return subprocess.CompletedProcess(
                    cmd, 1, b"",
                    b"scp: destination ne designe aucun hote simule",
                )
            deposition = correspondants[0]
        else:
            deposition = cibles[0]
        deposition.fichiers[_cle(chemin_cible)] = donnees
        return subprocess.CompletedProcess(cmd, 0, b"copie reussie", b"")

    return _run


def _cle(chemin: str) -> str:
    """Forme canonique d'un chemin dans le registre d'un hote simule.

    Le script distant lit le chemin tel qu'il est pose dans `osd_arg2`
    et `osd_arg3`, et la ligne de commande le recompose. Les deux
    formes doivent designer la meme cle, sans quoi la fausse copie
    chercherait un fichier que le faux runner a bien pose.
    """
    return "/" + "/".join(p for p in chemin.split("/") if p)


def backend(
    *,
    source: Optional[RunnerFaux] = None,
    target: Optional[RunnerFaux] = None,
    mode: str = "auto",
    cache: Optional[Path] = None,
    ssh_password: str = "",
) -> TransferBackend:
    """Construit un `TransferBackend` sur deux faux hotes.

    `ssh_password` est expose parce que le comportement du transfert
    differe selon le mode d'authentification : par cle, `BatchMode=yes`
    bloque toute invite ; par mot de passe, il faut au contraire
    l'autoriser et poser `NumberOfPasswordPrompts=1`. Les deux branches
    meritent d'etre exercees, faute de quoi la seconde ne serait
    testee que par le chemin reel, sur une machine de l'exploitant.
    """
    return TransferBackend(
        source_runner=source or RunnerFaux(label="source", host="src.exemple"),
        target_runner=target or RunnerFaux(label="cible", host="tgt.exemple"),
        mode=mode,
        ssh_password=ssh_password,
        probe_cache_dir=cache,
    )


class SansReseau:
    """Remplace `transfer._run_command` le temps d'un test.

    Un gestionnaire de contexte, et non un decorateur : la portee doit
    etre evidente a la lecture, parce qu'une substitution de `run` trop
    large transformerait silencieusement un test en test qui ne fait
    rien. Le `finally` rend l'oubli impossible.
    """

    def __init__(
        self,
        source: RunnerFaux,
        cible: Any,
        *,
        vues: Optional[List[List[str]]] = None,
        **kw: Any,
    ) -> None:
        self._avant = tr._run_command
        faux = _copie_simulee(source, cible, **kw)
        if vues is None:
            self._faux = faux
        else:
            # Enregistrement **avant** simulation : le test lit les
            # commandes meme quand elles echouent, et c'est meme alors
            # qu'il doit le plus en lire — une commande qui n'a pas ete
            # emise est aussi un constat, et un constat qu'aucun message
            # d'erreur ne porte.
            def _capture(
                cmd: Sequence[str], **opts: Any
            ) -> subprocess.CompletedProcess:
                vues.append(list(cmd))
                return faux(cmd, **opts)

            self._faux = _capture
        self.source = source
        self.cible = cible

    def __enter__(self) -> "SansReseau":
        tr._run_command = self._faux
        return self

    def __exit__(self, *exc: Any) -> None:
        tr._run_command = self._avant


class TestSelectionDeTopologie(unittest.TestCase):
    """Quand aucun transfert n'a lieu, et pourquoi."""

    def test_deux_cotes_locaux_ne_requierent_aucune_sonde(self):
        """Pas de SSH, pas de sonde : le chemin est court-circuite.

        Sonder quand meme coute un aller-retour pour rien, et pourrait
        echouer sur une machine parfaitement fonctionnelle — dont on
        pretendrait ensuite qu'elle est le probleme.
        """
        local = RunnerFaux(kind="local", label="local")
        be = backend(source=local, target=local)
        self.assertEqual(be.resolve(src_dir="/d", dst_dir="/d"), "local")
        self.assertEqual(local.operations, [])

    def test_un_mixte_local_et_distant_est_refuse(self):
        """Un cote local, un distant : le transfert n'a pas de forme.

        `SOURCE_HOST` vide avec `TARGET_HOST` renseigne est saisissable —
        `config.example.conf` previent le cas. `expdp` ecrit alors le
        dump sur le serveur de saut, `impdp` le lit sur l'hote distant
        qui ne voit pas ce systeme de fichiers, et l'import echoue sur
        un `ORA-39000` **trois etapes plus loin**, avec pour seul indice
        un dump parfaitement valide cote source.

        Le refus doit donc nommer la topologie, et non dire « aucun
        transfert » : c'est la difference entre un diagnostic qu'on peut
        corriger et une panne qu'on cherche ailleurs.
        """
        local = RunnerFaux(kind="local", label="source")
        cible = RunnerFaux(label="cible")
        be = backend(source=local, target=cible)
        with SansReseau(local, cible):
            with self.assertRaises(TransferError) as contexte:
                be.resolve(src_dir="/d", dst_dir="/d")
        self.assertIn("mixte", str(contexte.exception))
        # Le remede nomme le mecanisme, pas la cle : cette couche ne
        # connait pas les noms de configuration. C'est la validation de
        # configuration, en amont, qui nomme `SOURCE_HOST` exactement —
        # les deux messages se completent au lieu de se concurrencer.
        self.assertIn("_HOST", str(contexte.exception.hint))
        detail = " ".join(contexte.exception.detail)
        self.assertIn("local", detail)
        self.assertIn("cible", detail)

    def test_le_mixte_est_refuse_dans_les_deux_sens(self):
        """Source distante, cible locale : le meme piege, symetrique.

        L'asymetrie serait facile a introduire par megarde, puisque le
        sens « source distante » est le cas nominal. Le test fixe les
        deux.
        """
        source = RunnerFaux(label="source")
        cible = RunnerFaux(kind="local", label="cible")
        be = backend(source=source, target=cible)
        with SansReseau(source, cible):
            with self.assertRaises(TransferError):
                be.resolve(src_dir="/d", dst_dir="/d")

    def test_les_deux_cotes_locaux_donnent_la_topologie_partage(self):
        self.assertEqual(RunnerFaux(kind="local").kind, "local")
        be = backend(source=RunnerFaux(kind="local"), target=RunnerFaux(kind="local"))
        self.assertEqual(be._topologie(), "partage")

    def test_deux_cotes_distants_donnent_la_topologie_relais(self):
        be = backend(source=RunnerFaux(), target=RunnerFaux())
        self.assertEqual(be._topologie(), "relais")

    def test_un_mode_local_impose_neant_meme_si_les_cotes_sont_distants(self):
        self.assertEqual(backend(mode="local").resolve(src_dir="/d", dst_dir="/d"), "local")

    def test_le_mode_est_normalise_en_minuscules(self):
        """`TRANSFER_MODE` est un enum en majuscules en configuration.

        La conversion se fait en un seul point plutot que dans chaque
        comparaison, ce qui evite qu'un mode invalide passe pour `auto`
        selon la comparaison utilisee — donc qu'il soit traite en
        transfert alors que l'exploitant voulait autre chose.
        """
        self.assertEqual(backend(mode="LOCAL").resolve(src_dir="/d", dst_dir="/d"), "local")

    def test_le_resultat_est_memoise(self):
        """Une seule sonde, meme en cas d'appel repete.

        L'etape 13 appelle `resolve`, puis `run` appelle `resolve` a
        nouveau. Sonder deux fois coutait deux allers-retours, et le
        second pouvait reussir la ou le premier avait echoue — donc
        changer de backend en cours de route, de maniere non
        reproductible.
        """
        source = RunnerFaux(label="source")
        cible = RunnerFaux(label="cible")
        be = backend(source=source, target=cible, mode="scp")
        with SansReseau(source, cible):
            be.resolve(src_dir="/d", dst_dir="/d")
            avant = len(source.operations)
            be.resolve(src_dir="/d", dst_dir="/d")
        self.assertEqual(len(source.operations), avant)
        self.assertTrue(avant, "aucune sonde n'a eu lieu : le test ne prouve rien")


class TestSondeDeCapacite(unittest.TestCase):
    """Les quatre operations, et l'ordre dans lequel elles ont lieu.

    Une sonde qui oublie le nettoyage laisse un `.osd-probe-*` sur les
    hotes a chaque tentative. Quatre tentatives a chaque run : le
    serveur se remplit de temoins que personne ne reconnait.
    """

    def setUp(self) -> None:
        self.source = RunnerFaux(label="source", host="src.exemple")
        self.cible = RunnerFaux(label="cible", host="tgt.exemple")
        self.be = backend(source=self.source, target=self.cible)

    def test_une_sonde_reussie_ebauche_les_quatre_operations(self):
        """Ecrire, copier, verifier, nettoyer — dans cet ordre.

        L'ordre n'est pas decoratif : verifier avant d'avoir copie
        reussirait toujours, puisque la cible ne contiendrait rien et
        que le temoin y serait justement attendu ; nettoyer avant
        d'avoir verifie supprimerait la preuve.
        """
        with SansReseau(self.source, self.cible):
            ok, raison = self.be._probe("scp", "/src", "/dst")
        self.assertTrue(ok, raison)
        self.assertEqual([op[0] for op in self.source.operations], ["write", "remove"])
        self.assertEqual([op[0] for op in self.cible.operations], ["verify", "remove"])

    def test_les_deux_cotes_sont_nettoyes_meme_en_cas_d_echec(self):
        """Le nettoyage est dans un `finally`, pas dans le chemin heureux.

        C'est le seul endroit du module qui le garantit, et il est
        invisible a la lecture du chemin nominal. Un test qui ne verifie
        que le succes passerait meme apres suppression du `finally`.
        """
        with SansReseau(self.source, self.cible, echecs=("scp",)):
            self.assertFalse(self.be._probe("scp", "/src", "/dst")[0])
        self.assertIn("remove", [op[0] for op in self.source.operations])
        self.assertIn("remove", [op[0] for op in self.cible.operations])
        self.assertEqual(self.source.chemins(), [])
        self.assertEqual(self.cible.chemins(), [])

    def test_un_echec_a_l_ecriture_interdit_toute_copie(self):
        """Ecrire le temoin puis tenter de le transferer n'a pas de sens.

        Le motif doit etre precis : « copie refusee » ferait chercher
        du cote SSH alors que la source refuse l'ecriture — souvent
        parce que le repertoire du `DIRECTORY` Oracle a un proprietaire
        different de celui de l'utilisateur de transfert.
        """
        self.source.echec = ("write", 28)
        with SansReseau(self.source, self.cible):
            ok, raison = self.be._probe("scp", "/src", "/dst")
        self.assertFalse(ok)
        self.assertIn("ecriture", raison)
        # L'invariant est « aucune copie tentee » : ni ecriture ni
        # verification cote cible. Le `remove` y figure quand meme,
        # parce que le `finally` nettoie les deux cotes sans condition —
        # ce qui est le seul endroit ou l'on peut garantir qu'un temoin
        # laisse par une ecriture interrompue ne survivra pas.
        self.assertEqual(
            [op[0] for op in self.cible.operations if op[0] != "remove"], []
        )
        self.assertEqual([op[0] for op in self.source.operations], ["write", "remove"])
        self.assertEqual(self.source.chemins(), [])
        self.assertEqual(self.cible.chemins(), [])

    def test_un_fichier_absent_apres_copie_est_un_echec_de_sonde(self):
        """Un succes de `scp` sans fichier n'est pas un succes.

        C'est le cas de l'espace insuffisant cote cible, signale par un
        code 0 selon la version du client. Sans cette verification, le
        transfert « reussirait » et l'import echouerait trois etapes
        plus loin sur un `ORA-39000`, avec pour seul indice un fichier
        absent — et un rapport qui pretend le contraire.
        """
        # La copie repond 0 **sans rien deposer** : c'est le cas que la
        # verification existe pour attraper. Une copie qui echoue
        # testerait le chemin d'echec du client, deja couvert.
        with SansReseau(self.source, self.cible, silencieux=True):
            ok, raison = self.be._probe("scp", "/src", "/dst")
        self.assertFalse(ok)
        self.assertIn("absent ou altere", raison)

    def test_la_sonde_utilise_les_repertoires_reels(self):
        """Pas `/tmp` : le repertoire du `DIRECTORY` qui sera transféré.

        Un `DIRECTORY` Oracle est souvent monte avec un proprietaire ou
        des permissions differents de `/tmp`. Une sonde qui reussit la ou
        le vrai transfert echouerait produit un diagnostic faux, et le
        remede qu'en tire l'exploitant — changer de mecanisme — rend la
        situation **pire**, puisque le mecanisme choisi ne sera pas
        sonde non plus.
        """
        self.source.probe_dir = "/donnees/avec/permissions"
        self.cible.probe_dir = "/autre/dossier"
        with SansReseau(self.source, self.cible):
            self.be._probe("scp", "", "")
        ecrits = [c for a, c in self.source.operations if a == "write"]
        self.assertTrue(ecrits, "aucune ecriture enregistree")
        for chemin in ecrits:
            self.assertIn("/donnees/avec/permissions", chemin)
            self.assertNotIn("/tmp/", chemin)

    def test_les_repertoires_explicites_prime_sur_ceux_du_runner(self):
        """Ce qui est passe a la sonde gagne sur `probe_dir`.

        Le transfert a lieu dans le `DIRECTORY` resolu, pas dans le
        `probe_dir` par defaut. Sonder ailleurs validerait un chemin que
        le transfert n'empruntera pas.
        """
        with SansReseau(self.source, self.cible):
            self.be._probe("scp", "/reel/source", "/reel/cible")
        ecrits = [c for a, c in self.source.operations if a == "write"]
        self.assertTrue(all(c.startswith("/reel/source") for c in ecrits), ecrits)

    def test_un_rsync_absent_du_hote_source_est_nomme(self):
        """Le message doit dire **ou** il manque.

        Deux hotes, deux installations : « rsync absent » sans designer
        le cote ne fait avancer personne, et le remedy — installer sur
        la bonne machine — est precis.
        """
        self.source.host_binaries = set()
        manquants = self.be._missing_binaries("rsync")
        self.assertTrue(any("source" in m for m in manquants), manquants)
        self.assertFalse(any("cible" in m for m in manquants), manquants)


class TestCacheDeSonde(unittest.TestCase):
    """Le cache, sa duree de validite, et ce qu'il ne doit pas contenir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name)
        self.source = RunnerFaux(label="source", host="src.exemple")
        self.cible = RunnerFaux(label="cible", host="tgt.exemple")

    def test_une_sonde_reussie_est_memorisee(self):
        with SansReseau(self.source, self.cible):
            backend(source=self.source, target=self.cible, mode="scp",
                    cache=self.cache).resolve(src_dir="/d", dst_dir="/d")
        fichiers = list(self.cache.iterdir())
        self.assertEqual(len(fichiers), 1, fichiers)
        self.assertTrue(fichiers[0].name.startswith("probe-"))

    def test_une_sonde_memoisee_evite_un_second_aller_retour(self):
        """Seul le cache disque compte, pas la memoire de l'instance.

        Le test cree un **nouveau** backend. Sans cela il passerait meme
        avec un cache totalement inoperant, puisque `resolve`
        memoise dans l'objet.
        """
        with SansReseau(self.source, self.cible):
            backend(source=self.source, target=self.cible, mode="scp",
                    cache=self.cache).resolve(src_dir="/d", dst_dir="/d")
            avant = len(self.source.operations)
            fresh = backend(source=self.source, target=self.cible, mode="scp",
                            cache=self.cache)
            self.assertEqual(fresh.resolve(src_dir="/d", dst_dir="/d"), "scp")
        self.assertEqual(len(self.source.operations), avant)

    def test_un_cache_expire_redispose_la_sonde(self):
        """Un `sshd` peut etre reconfigure entre deux runs.

        Un cache d'une journee conduirait a un echec de transfert
        inexplique des heures apres la cause. La duree est donc courte,
        et surtout **testee** : un cache sans expiration est un cache
        qui ment, et qui ment silencieusement.
        """
        with SansReseau(self.source, self.cible):
            backend(source=self.source, target=self.cible, mode="scp",
                    cache=self.cache).resolve(src_dir="/d", dst_dir="/d")
            for fichier in self.cache.iterdir():
                age = PROBE_TTL_S + 60
                os.utime(fichier, (time.time() - age, time.time() - age))
            avant = len(self.source.operations)
            backend(source=self.source, target=self.cible, mode="scp",
                    cache=self.cache).resolve(src_dir="/d", dst_dir="/d")
        self.assertGreater(len(self.source.operations), avant)

    def test_une_sonde_echouee_n_est_pas_memoisee(self):
        """Memoriser un echec figerait l'echec pour une heure.

        Le remede — installer `rsync`, accepter la cle d'hote — peut etre
        applique en deux minutes. Un cache d'echec rendrait le correctif
        invisible et le run echouerait encore, ce qui est le pire
        message possible a envoyer.
        """
        with SansReseau(self.source, self.cible, echecs=("scp",)):
            be = backend(source=self.source, target=self.cible, mode="scp",
                         cache=self.cache)
            with self.assertRaises(TransferError):
                be.resolve(src_dir="/d", dst_dir="/d")
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_la_cle_de_cache_ne_contient_ni_separateur_ni_espace(self):
        """Un nom d'hote peut contenir `/`, `:` et des espaces.

        Sans assainissement, la cle produirait soit un
        sous-repertoire inexistant — donc un cache **toujours** vide, et
        une sonde a chaque run — soit une collision entre deux paires
        d'hotes, donc un transfert declare capable sur une paire qui ne
        l'est pas.
        """
        source = RunnerFaux(label="source", host="a/b:c d")
        cible = RunnerFaux(label="cible", host="e/f:g h")
        with SansReseau(source, cible):
            backend(source=source, target=cible, mode="scp",
                    cache=self.cache).resolve(src_dir="/d", dst_dir="/d")
        chemin = list(self.cache.iterdir())[0]
        self.assertEqual(chemin.parent, self.cache)
        for caractere in "/: ":
            self.assertNotIn(caractere, chemin.name)

    def test_deux_paires_d_hotes_ne_partagent_pas_leur_cache(self):
        """La cle doit distinguer les paires, pas seulement le backend.

        Deux duplications vers deux cibles depuis la meme source doivent
        pouvoir repondre a deux questions de capacite differentes. Une
        collision ferait heriter a la cible B la reponse obtenue pour A.
        """
        avec_a = RunnerFaux(label="cible", host="a.exemple", probe_dir="/donnees/a")
        avec_b = RunnerFaux(label="cible", host="b.exemple", probe_dir="/donnees/b")
        with SansReseau(self.source, [avec_a, avec_b]):
            self.assertEqual(
                backend(source=self.source, target=avec_a, mode="scp",
                        cache=self.cache).resolve(
                            src_dir=self.source.probe_dir, dst_dir=avec_a.probe_dir),
                "scp",
            )
            self.assertEqual(
                backend(source=self.source, target=avec_b, mode="scp",
                        cache=self.cache).resolve(
                            src_dir=self.source.probe_dir, dst_dir=avec_b.probe_dir),
                "scp",
            )
        self.assertEqual(len(list(self.cache.iterdir())), 2)
        # Les deux sondes ont reellement eu lieu : sans cela, deux cles
        # pourraient provenir d'une seule.
        self.assertTrue(avec_a.operations and avec_b.operations)

    def test_sans_cache_la_sonde_est_refaite_a_chaque_fois(self):
        """L'absence de cache ne doit pas degrader le resultat.

        Le cache est une **optimisation**. Le supprimer — parce qu'aucun
        `WORK_DIR` n'est configuré, par exemple — ne doit changer ni le
        backend retenu, ni le succes.
        """
        cible = RunnerFaux(label="cible")
        with SansReseau(self.source, cible):
            be = backend(source=self.source, target=cible, mode="scp", cache=None)
            self.assertEqual(be.resolve(src_dir="/d", dst_dir="/d"), "scp")


class TestLignesDeCommande(unittest.TestCase):
    """La construction des commandes, sans les executer.

    L'unicite des options est une propriete de la ligne de commande
    elle-meme : `-e` n'accepte qu'une valeur, et une option repetee est
    silencieusement ignoree. Le test doit donc le voir.
    """

    def setUp(self) -> None:
        self.source = RunnerFaux(label="source", host="src.exemple", user="oracle")
        self.cible = RunnerFaux(label="cible", host="tgt.exemple", user="admin")
        self.be = backend(source=self.source, target=self.cible)

    def test_rsync_ne_pose_qu_un_seul_dash_e(self):
        """Toutes les options SSH tiennent dans un seul `-e`.

        `-e` est a valeur unique. Le code emettait `-e "ssh opt1" -e
        "ssh opt2" ...`, dont seule la derniere valeur comptait :
        `BatchMode=yes` et `ConnectTimeout` etaient perdus. Le
        transfert echouait alors sur une invite de mot de passe — sous
        cron, un run bloque jusqu'a expiration du crontab, avec un
        symptome (« ca marche a la main ») qui ne renvoie vers aucun
        des reglages poses.
        """
        self.source.ssh_opts = ["ConnectTimeout=30", "StrictHostKeyChecking=yes"]
        argv = self.be._rsync_command("/src", "/dst", "f.dmp", self.source.ssh_opts)
        self.assertEqual(argv.count("-e"), 1, argv)
        valeur = argv[argv.index("-e") + 1]
        self.assertTrue(valeur.startswith("ssh "), valeur)
        for attendue in ("BatchMode=yes", "ConnectTimeout=30", "StrictHostKeyChecking=yes"):
            self.assertIn(attendue, valeur)

    def test_rsync_omet_dash_e_sans_option(self):
        """Pas d'option, pas de `-e` : un `-e ssh` nu n'apporte rien.

        Il ne serait pas errone, mais il eclaire le lecteur du journal
        alors qu'aucun choix n'a ete fait — et la ligne de commande est
        justement ce que l'on copie dans un ticket.
        """
        argv = self.be._rsync_command("/src", "/dst", "f.dmp", [])
        # `BatchMode` est toujours ajoute, donc `-e` reste present ; le
        # controle porte sur l'absence d'option vide.
        self.assertNotIn("", argv)
        valeur = argv[argv.index("-e") + 1]
        self.assertEqual(valeur, "ssh -o BatchMode=yes")

    def test_rsync_prefixe_chaque_option_par_son_o(self):
        """`rsync` scinde la valeur de `-e` sur les espaces.

        C'est le point qui a rendu `rsync` inutilisable en mode distant,
        et le defaut etait invisible : l'echec « Could not resolve
        hostname connecttimeout=10 » ne parle que de resolution de nom,
        alors que la cause est une ligne de commande mal construite. Le
        repli automatique vers `scp` le rendait de surcroi invisible.

        Le test porte sur la **forme exacte**, parce que c'est elle que
        `rsync` decoupe : `ssh`, puis autant de paires `-o`/`valeur`
        qu'il y a d'options, sans quoi le premier mot apres `ssh` est
        pris pour le nom d'hote.
        """
        self.source.ssh_opts = ["ConnectTimeout=30", "StrictHostKeyChecking=yes"]
        argv = self.be._rsync_command("/src", "/dst", "f.dmp", self.source.ssh_opts)
        mots = argv[argv.index("-e") + 1].split()
        self.assertEqual(mots[0], "ssh")
        self.assertEqual(mots[1::2], ["-o"] * (len(mots[1:]) // 2), mots)
        # Aucun mot nu ne peut donc etre pris pour un nom d'hote.
        for mot in mots[2::2]:
            self.assertIn("=", mot, f"option sans valeur : {mot}")

    def test_scp_et_sftp_posent_une_option_par_occurrence(self):
        """`-o` se repete legitimement, contrairement a `-e`.

        La difference est le point : poser `BatchMode` une seule fois
        n'est pas indifferent entre les deux clients, et l'inverse est
        vrai aussi. Le test fixe les deux formes, pour qu'un
        « nettoyage » ulterieur ne les homogenise pas.
        """
        self.source.ssh_opts = ["ConnectTimeout=30"]
        argv = self.be._scp_command("/src", "/dst", "f.dmp", self.source.ssh_opts, legacy=False)
        self.assertEqual(argv.count("-o"), 2, argv)
        self.assertIn("ConnectTimeout=30", argv)
        self.assertIn("BatchMode=yes", argv)

    def test_scp_legacy_pose_l_option_O_avant_les_options(self):
        """`-O` doit venir avant les options, pas apres.

        Un reordonnancement le glisser apres une paire `-o` le ferait
        passer pour un argument de l'option, et `scp` refuserait alors le
        fichier a copier.

        Le test se comporte differemment selon la machine : quand `-O`
        est disponible il verifie l'ordre, sinon il verifie que
        l'absence est **signalee** plutot que de laisser `scp` echouer
        sur une option inconnue — dont le message, sur certaines
        versions, parle de fichier introuvable et envoie vers le
        mauvais diagnostic.
        """
        try:
            argv = self.be._scp_command("/src", "/dst", "f.dmp", [], legacy=True)
        except TransferError as erreur:
            # Comportement attendu sur un client trop ancien.
            self.assertIn("-O", str(erreur))
            self.assertIn("OpenSSH", str(erreur.detail))
            self.assertIn("TRANSFER_MODE", str(erreur.hint))
            return
        self.assertIn("-O", argv)
        # L'ordre **relatif** compte, pas un indice fixe : `-3` precede
        # `-O` quand les deux sont presents, et le test doit passer sur
        # les deux machines. Ce qui tromperait, c'est `-O` apres une
        # paire `-o` : `scp` le prendrait pour la valeur de l'option.
        self.assertLess(argv.index("-O"), argv.index("-o"))

    def test_batchmode_est_toujours_ajoute(self):
        """Sans lui, l'echec d'authentification ouvre une invite.

        Sous cron, cette invite ne sera jamais repondue : le processus
        reste bloque jusqu'a expiration du crontab, sans journal ni code
        de sortie. C'est la panne la plus couteuse en temps de
        diagnostic, et la plus facile a eviter.
        """
        self.assertIn("BatchMode=yes", _merge_opts([], ""))
        self.assertIn("BatchMode=yes", _merge_opts(["BatchMode=no"], ""))

    def test_batchmode_est_pose_une_seule_fois(self):
        """Doubler l'option rendrait le comportement non deterministe.

        `-o BatchMode=yes -o BatchMode=no` laisse la valeur retenue
        dependre de l'ordre, donc de la version du client.
        """
        opts = _merge_opts(["BatchMode=yes", "ConnectTimeout=5"], "")
        self.assertEqual([o for o in opts if o.startswith("BatchMode")], ["BatchMode=yes"])

    def test_batchmode_non_est_pas_ecarte_par_la_configuration(self):
        """`BatchMode=no` est remplace, pas respecte.

        La validation de configuration verifie la valeur, mais
        `_merge_opts` est aussi atteignable par des appelants qui n'y
        passent pas. `BatchMode=no` ouvre une invite de mot de passe qui,
        sous cron, bloque le run jusqu'a l'expiration du crontab — sans
        journal, sans code de sortie, et sans que l'exploitant puisse
        relier le symptome a sa cause.

        Le test fixe donc la garantie dans la fonction, et pas seulement
        dans la validation : c'est le seul endroit ou elle est verifiable
        sans monter une configuration complete.
        """
        for valeur in ("no", "NO", "askpass", "oui"):
            with self.subTest(valeur=valeur):
                opts = _merge_opts([f"BatchMode={valeur}", "ConnectTimeout=5"], "")
                self.assertEqual(
                    [o for o in opts if o.lower().startswith("batchmode")],
                    ["BatchMode=yes"],
                )
                self.assertIn("ConnectTimeout=5", opts)

    def test_la_source_est_designee_par_son_utilisateur_et_son_hote(self):
        """`scp` est un client du serveur de saut.

        Il lui faut donc `utilisateur@hote`, pas le chemin local. Un
        transfert « reussi » vers un fichier local deposerait le dump
        sur le serveur de saut et l'import echouerait ensuite sur un
        fichier absent, sans qu'aucune trace ne dise que le dump s'est
        arrete au milieu du chemin.
        """
        argv = self.be._scp_command("/src", "/dst", "f.dmp", [], legacy=False)
        self.assertEqual(argv[-2], "oracle@src.exemple:/src/f.dmp")
        self.assertEqual(argv[-1], "admin@tgt.exemple:/dst/f.dmp")

    def test_la_cible_est_aussi_un_hote_distant(self):
        """Les **deux** cotes sont distants, et doivent l'etre tous les deux.

        La source portait `utilisateur@hote:` ; la cible etait un chemin
        nu. En topologie `relais` — les deux cotes distants, la seule
        topologie de transfert — ce chemin est local au serveur de saut,
        ou n'existe generalement pas. Le client echouait alors sur un
        message de fichier introuvable ou de droit refuse, qui ne parle
        que de la destination : l'exploitant verifiait des permissions
        sur le mauvais hote, et la cause — un chemin construit pour une
        machine qui ne recoit pas le fichier — ne se lisait nulle part.

        Le test fixe la forme des deux operandes, parce que c'est elle que
        `scp` interprets comme « ou aller chercher, ou deposer ».
        """
        argv = self.be._scp_command("/src", "/dst", "f.dmp", [], legacy=False)
        self.assertIn("@", argv[-1], f"cible non designee comme hote : {argv[-1]}")
        self.assertIn(":", argv[-1])

        argv = self.be._rsync_command("/src", "/dst", "f.dmp", [])
        self.assertEqual(argv[-2], "oracle@src.exemple:/src/f.dmp")
        self.assertEqual(argv[-1], "admin@tgt.exemple:/dst/f.dmp")

    def test_les_deux_cotes_designent_le_meme_hote_que_le_reste_du_run(self):
        """Le compte et l'adresse viennent de l'inventaire, comme pour Ansible.

        `_hote` lit `transfer_host`/`transfer_user`, que le runner tire de
        `ansible_host`/`ansible_user`. La cible passe donc par la meme
        fonction que la source : si Ansible parle `172.16.1.82` sous le
        compte `oracle`, `scp` y parle aussi. Deux designations
        divergentes deposeraient le dump la ou l'import ne le cherche pas.
        """
        source = RunnerFaux(label="source", host="inventaire", user="")
        source.transfer_host = "172.16.1.84"
        source.transfer_user = "oracle"
        cible = RunnerFaux(label="cible", host="inventaire", user="")
        cible.transfer_host = "172.16.1.82"
        cible.transfer_user = "oracle"
        be = backend(source=source, target=cible)
        argv = be._scp_command("/exp", "/imp", "f.dmp", [], legacy=False)
        self.assertEqual(argv[-2], "oracle@172.16.1.84:/exp/f.dmp")
        self.assertEqual(argv[-1], "oracle@172.16.1.82:/imp/f.dmp")

    def test_scp_force_les_deux_sauts_par_le_serveur_de_saut(self):
        """`-3` : le second saut part du serveur de saut, pas de la source.

        Sans `-3`, `scp` distant-a-distant fait ouvrir la seconde session
        `ssh` **par la source**, avec la cle de la source. Sur deux hotes
        qui n'ont aucune cle l'une de l'autre — la situation normale —
        l'echec est « Permission denied, please try again » : un message
        d'authentification qui ne dit pas que le chemin est bon et que
        seule la cle manque. Il envoie vers les mots de passe, alors que
        poser une cle entre les hotes serait une ouverture inutile.

        Avec `-3`, les deux sessions sont ouvertes depuis le serveur de
        saut, qui possede deja les deux cles. Le transfert est donc aussi
        controle, sans distribuer de cle entre les hotes.
        """
        argv = self.be._scp_command("/src", "/dst", "f.dmp", [], legacy=False)
        self.assertIn("-3", argv, argv)
        # L'option doit preceder les `-o` : `scp` analyse dans l'ordre, et
        # apres une paire d'arguments elle prendrait le chemin pour une
        # option.
        self.assertLess(argv.index("-3"), argv.index("-o"))

    def test_rsync_ne_necessite_pas_cette_option(self):
        """`rsync` fait deja passer le transfert par la machine qui lance.

        L'option serait sans effet, et la poser par reflexe pour
        « uniformiser » les deux commandes donnerait une ligne qui ne
        signifie rien. Le test fixe l'absence, pas seulement la presence
        chez `scp` : une correction ulterieure ne doit pas propager `-3`
        a une commande qui ne le comprend pas.
        """
        argv = self.be._rsync_command("/src", "/dst", "f.dmp", [])
        self.assertNotIn("-3", argv, argv)

    def test_sftp_est_refuse_en_relais_avec_sa_raison(self):
        """`sftp` ne peut pas deposer sur un hote distant.

        Son script ne connait qu'un seul hote : `get` y telecharge, et le
        second chemin est local au serveur de saut. Un `relais` n'a donc
        aucune forme en `sftp` -- le dump atterrirait sur le serveur de
        saut, la cible ne le verrait jamais, et l'import echouerait trois
        etapes plus loin sur un fichier absent.

        Le refus doit nommer la limite du client. Un echec de `sftp`
       rapperait « fichier absent » et enverrait vers les droits, alors
        que le chemin est faux par construction.
        """
        ok, raison = self.be._sftp_command("/src", "/dst", "f.dmp", [])
        self.assertFalse(ok)
        self.assertIn("sftp", raison)
        self.assertIn("distant", raison)
        # Le repertoire fautif est nomme : c'est lui que l'exploitant
        # doit pouvoir reconnaitre dans le rapport.
        self.assertIn("/dst", raison)

    def test_sftp_ne_declenche_aucun_processus(self):
        """Le refus doit etre anterieur a toute execution.

        Un `sftp` reellement lance deposerait le fichier sur le serveur de
        saut avant d'echouer sur une destination inexistante : le run
        laisserait un artefact derriere lui, et la sonde aurait reellement
        deplace quelque chose alors qu'elle est censee ne rien transferer.
        """
        with mock.patch.object(tr, "_run_command") as lanceur:
            ok, _ = self.be._sftp_command("/src", "/dst", "f.dmp", [])
        self.assertFalse(ok)
        lanceur.assert_not_called()

    def test_sans_utilisateur_le_nom_d_hote_suffit(self):
        """Une version emettait `@hote`, une autre non.

        Le rapport indiquait alors un hote inexistant, et l'erreur —
        « could not resolve hostname @src » — enverrait vers le DNS au
        lieu du compte. Les trois commandes doivent obeyir a la meme
        regle.
        """
        source = RunnerFaux(label="source", host="src.exemple", user="")
        be = backend(source=source, target=self.cible)
        self.assertEqual(_hote(source), "src.exemple")
        argv = be._scp_command("/src", "/dst", "f.dmp", [], legacy=False)
        self.assertNotIn("@", argv[-2])
        argv = be._rsync_command("/src", "/dst", "f.dmp", [])
        self.assertNotIn("@", argv[-2])

    def test_le_chemin_distant_est_construit_avec_une_barre(self):
        """`PurePosixPath`, parce que l'hote distant est sous AIX.

        Le serveur de saut est sous Linux, donc `os.path.join`
        fonctionnerait ici — et le test passerait a tort. Le controle
        fixe la forme, parce que la forme est le contrat.
        """
        argv = self.be._scp_command("/src/", "/dst/", "f.dmp", [], legacy=False)
        self.assertTrue(argv[-2].endswith("/src/f.dmp"), argv[-2])
        self.assertTrue(argv[-1].endswith("/dst/f.dmp"), argv[-1])

    def test_rsync_propose_une_reprise_de_transfert_interrompu(self):
        """`--partial` garde la partie deja transferee.

        Un dump de plusieurs giga-octets interrompu a 90 % represente
        des heures de travail. Sans `--partial`, la reprise repart de
        zero ; l'exploitant finit par declencher le transfert la nuit,
        et le retour d'information d'un seul echec.
        """
        argv = self.be._rsync_command("/src", "/dst", "f.dmp", [])
        self.assertIn("--partial", argv)

    def test_rsync_ne_recoit_aucune_option_reservee_au_demon(self):
        """`--contimeout` n'a de sens que face un demon rsync.

        En mode `rsync -e ssh` -- le seul que nous employons -- `rsync`
        **refuse** l'option et echoue : c'est ce qui rendait le backend
        `rsync` inutilisable, l'echec etant ensuite masque par le repli
        automatique vers `scp`. Le test porte sur la commande rendue,
        parce que c'est la seule forme que `rsync` verra.

        Le delai de connexion est reporte sur `ConnectTimeout`, dans les
        options SSH : une sonde contre un hote injoignable echoue en
        10 s et non en 120.
        """
        argv = self.be._rsync_command("/s", "/d", "f", ["ConnectTimeout=10"])
        self.assertNotIn("--contimeout=30", argv)
        for element in argv:
            self.assertFalse(
                element.startswith("--contimeout"),
                "option reservee au demon rsync dans une commande ssh",
            )
        # Le delai borne est bien present, par la voie qui fonctionne.
        self.assertIn("ConnectTimeout=10", " ".join(argv))

    def test_le_delai_de_connexion_vient_des_options_inventaire(self):
        """Sans option d'inventaire, `rsync` n'a pas de borne de connexion.

        Ce n'est pas une regression : l'inventaire fournit
        `ConnectTimeout=10` par defaut, et `_merge_opts` le conserve. Le
        test le fixe pour qu'un retrait futur de cette option soit vu.
        """
        from osd.adapters import transfer as module

        self.assertIn("ConnectTimeout=10", module._merge_opts(["ConnectTimeout=10"], ""))


class TestRetenueEnDryRun(unittest.TestCase):
    """Le dry-run doit refuser le transfert, et le dire.

    `--dry-run` qui transfere des giga-octets n'est pas lent : il est
    **faux**. Le rapport afficherait des octets transferes qui ne le
    sont pas, et l'exploitant croirait avoir valide un chemin qui n'a
    jamais ete exerce.
    """

    def test_un_backend_sans_mutation_ne_transfere_rien(self):
        source = RunnerFaux(label="source", allows_mutation=False)
        be = backend(source=source, target=RunnerFaux(), mode="scp")
        with SansReseau(source, RunnerFaux()):
            self.assertFalse(
                be._run_backend("scp", "/src", "/dst", "f.dmp", probe=False)
            )

    def test_la_refus_porte_sur_les_quatre_backends(self):
        """Un seul chemin refuse, les autres passeraient.

        `sftp` et `rsync` ne passent pas par le meme branche de
        `_run_backend`. La retenue doit preceder la construction de la
        commande, sinon l'un d'eux s'echapperait — et c'est
        precisement le backend que la machine de developpement possede.
        """
        source = RunnerFaux(label="source", allows_mutation=False)
        cible = RunnerFaux(label="cible")
        be = backend(source=source, target=cible)
        with SansReseau(source, cible):
            for nom in AUTO_ORDER:
                with self.subTest(backend=nom):
                    self.assertFalse(
                        be._run_backend(nom, "/src", "/dst", "f.dmp", probe=False)
                    )

    def test_la_sonde_echoue_sans_mutation(self):
        """Aucun backend ne peut etre retenu, donc `resolve` echoue.

        C'est le comportement honnete : plutot que d'inventer un backend
        et d'affirmer un transfert qui n'a pas eu lieu, le rapport dit
        que rien n'a ete transfere, et pourquoi.
        """
        source = RunnerFaux(label="source", allows_mutation=False)
        cible = RunnerFaux(label="cible")
        be = backend(source=source, target=cible, mode="scp")
        with SansReseau(source, cible):
            with self.assertRaises(TransferError) as contexte:
                be.resolve(src_dir="/src", dst_dir="/dst")
        self.assertIn("scp", str(contexte.exception.detail))

    def test_le_null_runner_refuse_la_mutation(self):
        """Le controle passe par le runner, pas par un `if dry_run`.

        Un `if dry_run` disperse finit inevitably par oublier un chemin —
        ici, celui-ci. En passant par `allows_mutation()`, la decision
        reste au runner, seul composant qui sache s'il simule, et le
        test se limite a verifier que les deux repondent juste.
        """
        reel = LocalRunner()
        simule = NullRunner(reel, "transfert simule")
        self.assertTrue(reel.allows_mutation())
        self.assertFalse(simule.allows_mutation())


class TestTransfertSurRepertoirePartage(unittest.TestCase):
    """Deux cotes qui voient le meme repertoire.

    Aucun mecanisme de copie n'est appele, et c'est la seule forme de
    transfert qui n'en ait pas. Le cas etait donc le plus expose : la
    resolution renvoie `local`, qui n'est pas dans `AUTO_ORDER` et que
    `_run_backend` ne connait pas — l'echec se lisait « backend
    inconnu: local », sur un run dont le dump etait deja en place.

    L'appelant ne pouvait rien voir : la topologie est connue du seul
    `resolve()`, et lire le mode configure ne la donnait pas.
    """

    def setUp(self) -> None:
        self.source = RunnerFaux(label="source", kind="local")
        self.cible = RunnerFaux(label="cible", kind="local")
        self.source.fichiers["/reel/base.dmp"] = b"x" * 4096
        # Aucun mode force : c'est le cas par defaut d'une duplication
        # sur un serveur de saut, ou `TRANSFER_MODE` n'a pas lieu d'etre
        # pose puisque les deux cotes sont locales.
        self.be = backend(source=self.source, target=self.cible)

    def test_aucun_client_de_copie_n_est_appele(self):
        with SansReseau(self.source, self.cible):
            outcome = self.be.run(
                src_dir="/reel", dst_dir="/reel", names=["base.dmp"], job_name="J",
            )
        self.assertEqual(outcome.method, "partage")
        self.assertEqual(outcome.backend, "local")
        # Aucune sonde non plus : il n'y a rien a mesurer sur un
        # mecanisme qui n'est pas employe. Les seules lectures admises
        # sont les mesures de volume, et aucune ecriture n'a lieu.
        actions = {action for action, _ in self.source.operations}
        actions |= {action for action, _ in self.cible.operations}
        self.assertTrue(actions <= {"size"}, f"operations inattendues : {actions}")

    def test_le_compte_rendu_annonce_le_volume_du_dump(self):
        """Un transfert nul n'est pas un dump de volume nul.

        Le rapport compare ce volume a la place disponible, a l'etape 9.
        Un compte rendu a zero y ferait conclure que rien n'a ete
        transporte — donc que le dump peut etre efface — alors qu'il
        occupe le repertoire partage.
        """
        with SansReseau(self.source, self.cible):
            outcome = self.be.run(
                src_dir="/reel", dst_dir="/reel", names=["base.dmp"], job_name="J",
            )
        self.assertEqual(outcome.files, ["base.dmp"])
        self.assertEqual(outcome.bytes_total, 4096)

    def test_toutes_les_parties_sont_annoncees(self):
        """Une serie de parties reste une serie, meme sans copie."""
        for i in range(4):
            self.source.fichiers[f"/reel/base-{i}.dmp"] = b"y" * 1024
        with SansReseau(self.source, self.cible):
            outcome = self.be.run(
                src_dir="/reel", dst_dir="/reel",
                names=[f"base-{i}.dmp" for i in range(4)], job_name="J",
            )
        self.assertEqual(len(outcome.files), 4)
        self.assertEqual(outcome.bytes_total, 4096)

    def test_un_mode_local_explicite_donne_le_meme_compte_rendu(self):
        """Poser `TRANSFER_MODE=local` ne doit rien changer d'autre.

        Les deux formes aboutissent a la meme topologie, donc au meme
        compte rendu : les distinguer introduirait un ecart entre deux
        configurations qui decrivent la meme realite.
        """
        self.be = backend(source=self.source, target=self.cible, mode="local")
        with SansReseau(self.source, self.cible):
            outcome = self.be.run(
                src_dir="/reel", dst_dir="/reel", names=["base.dmp"], job_name="J",
            )
        self.assertEqual(outcome.method, "partage")
        self.assertEqual(outcome.bytes_total, 4096)


class TestTransfertReelSurFauxHotes(unittest.TestCase):
    """Le transfert complet, de bout en bout, sans reseau.

    Ces tests exercent `run()` : c'est le seul endroit ou la taille du
    fichier **apres** copie est verifiee, et cette verification est ce
    qui distingue un transfert reussit d'un `scp` qui a rendu 0.
    """

    def setUp(self) -> None:
        self.source = RunnerFaux(label="source", host="src.exemple", user="oracle")
        self.cible = RunnerFaux(label="cible", host="tgt.exemple", user="admin")
        self.source.fichiers["/reel/base.dmp"] = b"x" * 4096
        self.cible.echec = None
        self.be = backend(source=self.source, target=self.cible, mode="scp")

    def test_un_transfert_reussi_compte_les_octets(self):
        with SansReseau(self.source, self.cible):
            outcome = self.be.run(
                src_dir="/reel", dst_dir="/reprise",
                names=["base.dmp"], job_name="J",
            )
        self.assertEqual(outcome.files, ["base.dmp"])
        self.assertEqual(outcome.bytes_total, 4096)
        self.assertEqual(outcome.backend, "scp")
        self.assertEqual(outcome.method, "relais")

    def test_les_parties_du_dump_sont_transferees_une_par_une(self):
        """Le dump d'un export parallele est une **serie** de fichiers.

        Les `.dmp` d'un `PARALLEL=4` portent le jeton `%d`, et aucun nom
        n'est devinable. Les transferer en bloc — ou n'en transferer
        qu'un — produirait un import qui echoue sur `ORA-39059`, trois
        etapes plus loin, avec pour seul indice un fichier manquant.
        """
        for i in range(4):
            self.source.fichiers[f"/reel/base-{i}.dmp"] = b"y" * 1024
        with SansReseau(self.source, self.cible):
            outcome = self.be.run(
                src_dir="/reel", dst_dir="/reprise",
                names=[f"base-{i}.dmp" for i in range(4)], job_name="J",
            )
        self.assertEqual(len(outcome.files), 4)
        self.assertEqual(outcome.bytes_total, 4096)

    def test_un_fichier_vide_apres_copie_est_signale(self):
        """Le succes du client ne suffit pas : le fichier doit exister.

        Ce controle est le seul garde-fou contre un `scp` qui rend 0 sans
        avoir rien depose — le symptome d'un systeme de fichiers plein,
        ou d'un `DIRECTORY` dont le chemin physique a change entre la
        resolution et la copie.
        """
        with SansReseau(self.source, self.cible, echecs=("scp",)):
            with self.assertRaises(TransferError) as contexte:
                self.be.run(
                    src_dir="/reel", dst_dir="/reprise",
                    names=["base.dmp"], job_name="J",
                )
        self.assertIn("scp", str(contexte.exception.detail))

    def test_une_partie_manquante_interrompt_le_transfert(self):
        """Une partie absente doit etre signalee, pas ignoree.

        L'ignorer produirait un `dump_parts` partiel, puis un import
        qui echoue sur une partie manquante — diagnostic incomplet, a
        deux etapes de distance, et sans lien avec la cause.
        """
        with SansReseau(self.source, self.cible):
            with self.assertRaises(TransferError) as contexte:
                self.be.run(
                    src_dir="/reel", dst_dir="/reprise",
                    names=["base.dmp", "absent.dmp"], job_name="J",
                )
        self.assertIn("absent.dmp", str(contexte.exception))

    def test_le_echec_porte_le_motif_du_backend(self):
        """Le remede doit suivre du motif, pas du seul echec.

        « scp: permission denied » sans le nom du fichier conduit a
        verifier le repertoire entier, sur deux hotes.
        """
        with SansReseau(self.source, self.cible):
            with self.assertRaises(TransferError) as contexte:
                self.be.run(
                    src_dir="/reel", dst_dir="/reprise",
                    names=["absent.dmp"], job_name="J",
                )
        # Le message nomme le fichier ; le detail nomme la cause. Les
        # deux sont necessaires : « transfert impossible » conduit a
        # verifier les deux repertoires, et le nom du fichier seul conduit
        # a le chercher a un endroit ou il n'a jamais ete ecrit.
        self.assertIn("absent.dmp", str(contexte.exception))
        detail = " ".join(contexte.exception.detail)
        self.assertIn("absent", detail)
        self.assertIn("scp", detail.lower() + " ".join(contexte.exception.detail))


class TestTransfertEnDeuxSauts(unittest.TestCase):
    """Le transfert par mot de passe : deux invocations, jamais une.

    Mesure sur l'hote de saut : `scp -3` sous `sshpass` rend un rc 5 et
    un stderr vide — la seconde invite, trouvee dans le meme
    processus, est prise pour la preuve que le premier essai a ete
    refuse. Le remede n'est pas un autre client : deux commandes, une
    invitation chacune, et un depot local entre les deux.

    Le depot est **ecrit sur disque** par la fausse copie, parce que le
    code reel le supprime dans un `finally` : une suppression que rien
    n'a ecrite ne se voit pas, donc ne se teste pas.
    """

    def setUp(self) -> None:
        self.source = RunnerFaux(label="source", host="src.exemple", user="oracle")
        self.cible = RunnerFaux(label="cible", host="tgt.exemple", user="admin")
        self.source.fichiers["/reel/base.dmp"] = b"x" * 4096
        self.registre: Dict[str, bytes] = {}
        dossier = tempfile.TemporaryDirectory()
        self.addCleanup(dossier.cleanup)
        self.cache = Path(dossier.name) / "probes"
        self.be = backend(
            source=self.source, target=self.cible, mode="scp",
            cache=self.cache, ssh_password="secret",
        )
        self.vues: List[List[str]] = []

    def _sans_reseau(self, **kw: Any) -> SansReseau:
        """Sans reseau, en notant chaque commande emise."""
        return SansReseau(
            self.source, self.cible, vues=self.vues, local=self.registre, **kw
        )

    def test_le_secret_impose_deux_commandes_d_un_seul_hote_distant(self):
        """Une commande = une invitation. Le depot relie les deux.

        Chaque commande ne designe qu'un hote : `sshpass` y repond une
        fois. `-3` disparait parce qu'il tiendrait justement les deux
        invitations dans un meme processus — c'est precisement ce que
        `sshpass` ne sait pas faire.
        """
        self.assertTrue(self.be._staging_requis("scp"))
        with self._sans_reseau():
            ok = self.be._run_backend(
                "scp", "/reel", "/reprise", "base.dmp", probe=False
            )
        self.assertTrue(ok, self.be._last_reason)
        self.assertEqual(len(self.vues), 2, self.vues)
        descendre, monter = self.vues
        for commande in self.vues:
            self.assertEqual(commande[:3], ["sshpass", "-e", "scp"], commande)
            self.assertNotIn("-3", commande)
            self.assertIn("BatchMode=no", commande)
            self.assertIn("NumberOfPasswordPrompts=1", commande)
        # Un seul hote distant par invitation.
        self.assertEqual(descendre[-2], "oracle@src.exemple:/reel/base.dmp")
        self.assertNotIn("tgt.exemple", " ".join(descendre))
        self.assertNotIn("src.exemple", " ".join(monter))
        self.assertEqual(monter[-1], "admin@tgt.exemple:/reprise/base.dmp")
        # Le depot est le pont : le meme chemin, dans les deux commandes.
        depot = descendre[-1]
        self.assertEqual(monter[-2], depot)
        self.assertTrue(Path(depot).is_absolute())
        # Et le fichier arrive.
        self.assertEqual(self.cible.fichiers["/reprise/base.dmp"], b"x" * 4096)

    def test_un_transfert_par_cle_reste_en_une_seule_commande(self):
        """Pas de secret, pas de depot : `-3` tient toujours.

        La bifurcation porte sur le secret, et sur lui seul : sans
        invitation a repondre, la temporisation serait un cout paye pour
        rien, et un `-3` rendu absent romprait le transfert direct qui,
        lui, fonctionne.
        """
        sans_secret = backend(
            source=self.source, target=self.cible, mode="scp", cache=self.cache
        )
        self.assertFalse(sans_secret._staging_requis("scp"))
        self.assertTrue(self.be._staging_requis("scp"))

    def test_le_depot_ne_survit_pas_a_la_remontee(self):
        """Le dump ne doit pas rester sur le serveur de saut.

        Un depot oublie, c'est un dump visible de tous les comptes du
        serveur de saut, qui s'accumule d'un run a l'autre sans que
        rien ne le signale — et le dump contient les donnees du schema.
        """
        with self._sans_reseau():
            ok = self.be._run_backend(
                "scp", "/reel", "/reprise", "base.dmp", probe=False
            )
        self.assertTrue(ok, self.be._last_reason)
        depot = Path(self.vues[0][-1])
        self.assertFalse(depot.exists(), "le depot subsiste apres la copie")
        staging = self.cache.parent / "staging"
        self.assertEqual(list(staging.iterdir()), [])

    def test_un_echec_de_la_remontee_supprime_egalement_le_depot(self):
        """La seconde commande echoue : le depot part quand meme.

        C'est le cas ou l'oubli serait le plus probable — un `finally`
        execute alors que la variable d'etat de l'etape annonce un
        echec, et que l'envie de « laisser voir » est reelle pour le
        diagnostic. La regle est pourtant la meme dans les deux cas :
        rien ne reste sur le serveur de saut.
        """
        with self._sans_reseau(
            echecs=("admin@tgt.exemple:/reprise/base.dmp",)
        ):
            ok = self.be._run_backend(
                "scp", "/reel", "/reprise", "base.dmp", probe=False
            )
        self.assertFalse(ok)
        # Les deux invitations ont ete faites : c'est la seconde qui
        # echoue, et la premiere ne doit pas pour autant laisser un
        # depot derriere elle.
        self.assertEqual(len(self.vues), 2)
        self.assertIn("sous-systeme", self.be._last_reason)
        self.assertFalse(Path(self.vues[0][-1]).exists())
        self.assertNotIn("/reprise/base.dmp", self.cible.fichiers)

    def test_l_absence_de_place_est_dite_avant_la_premiere_copie(self):
        """Le controle est anterieur a toute copie.

        Sans lui, l'echec surviendrait a mi-parcours, signale par un
        message du systeme de fichiers qui ne nomme ni le fichier ni la
        topologie, et apres avoir occupe le double de la place
        necessaire. Le message dit ici la taille, la place et le
        repertoire — les trois element d'un defaut disque, dont aucun ne
        figure dans « No space left on device ».
        """
        with mock.patch(
            "shutil.disk_usage", return_value=mock.Mock(free=0, total=0, used=0)
        ):
            with self._sans_reseau():
                ok = self.be._run_backend(
                    "scp", "/reel", "/reprise", "base.dmp", probe=False
                )
        self.assertFalse(ok)
        self.assertEqual(self.vues, [], "une copie a ete tentee sans place")
        self.assertIn("place insuffisante", self.be._last_reason)
        self.assertIn("base.dmp", self.be._last_reason)
        self.assertIn("libres", self.be._last_reason)

    def test_rsync_en_deux_sauts_ne_designe_qu_un_hote_a_fois(self):
        """Le meme principe, pour le seul backend resumable.

        `rsync` fait passer le distant-a-distant par la machine qui le
        lance — deux sessions, donc deux invitations. Le depot est le
        meme que pour `scp` : c'est le mode d'authentification qui
        impose la forme, pas le client.
        """
        be = backend(
            source=self.source, target=self.cible, mode="rsync",
            cache=self.cache, ssh_password="secret",
        )
        with self._sans_reseau():
            ok = be._run_backend(
                "rsync", "/reel", "/reprise", "base.dmp", probe=False
            )
        self.assertTrue(ok, be._last_reason)
        self.assertEqual(len(self.vues), 2, self.vues)
        for commande in self.vues:
            self.assertEqual(commande[:3], ["sshpass", "-e", "rsync"], commande)
            # Le `-e` de `sshpass` est retranche : il ne porte pas les
            # options SSH, et les compterait deux fois.
            self.assertEqual(commande[3:].count("-e"), 1, commande)
            distants = [a for a in commande if "@" in a and ":" in a]
            self.assertEqual(len(distants), 1, commande)
        self.assertEqual(self.vues[0][-1], self.vues[1][-2])
        self.assertEqual(self.cible.fichiers["/reprise/base.dmp"], b"x" * 4096)

    def test_le_dry_run_ne_cree_meme_pas_le_depot(self):
        """La retenue precede le depot : un run simule ne laisse rien.

        Le depot est un effet de bord sur le serveur de saut. Un
        `--dry-run` qui en creerait un serait la seule trace d'une
        execution qui n'a rien fait — une trace qui, elle, s'accumule.
        """
        source = RunnerFaux(label="source", allows_mutation=False)
        be = backend(
            source=source, target=self.cible, mode="scp",
            cache=self.cache, ssh_password="secret",
        )
        with self._sans_reseau():
            self.assertFalse(
                be._run_backend("scp", "/reel", "/reprise", "base.dmp", probe=False)
            )
        self.assertEqual(self.vues, [])
        self.assertIn("dry-run", be._last_reason)
        self.assertFalse(
            (self.cache.parent / "staging").exists(),
            "le dry-run a cree le depot",
        )

    def test_le_transfert_complet_par_mot_de_passe_arrive_sans_residu(self):
        """Bout en bout : sonde et dump passent par le depot tous deux.

        La sonde ne doit pas valider un mecanisme different de celui du
        dump — sinon elle reussirait la ou le transfert echouerait, ce
        qui est le pire des diagnostics. Elle suit donc exactement le
        meme chemin : deux invitations par copie.
        """
        with self._sans_reseau():
            outcome = self.be.run(
                src_dir="/reel", dst_dir="/reprise",
                names=["base.dmp"], job_name="J",
            )
        self.assertEqual(outcome.files, ["base.dmp"])
        self.assertEqual(outcome.bytes_total, 4096)
        self.assertEqual(outcome.method, "relais")
        self.assertEqual(self.cible.fichiers["/reprise/base.dmp"], b"x" * 4096)
        dump = [c for c in self.vues if "base.dmp" in " ".join(c)]
        temoin = [c for c in self.vues if ".osd-probe-" in " ".join(c)]
        self.assertEqual(len(dump), 2, self.vues)
        self.assertEqual(len(temoin), 2, self.vues)
        staging = self.cache.parent / "staging"
        self.assertEqual(
            list(staging.iterdir()), [], "residu dans le depot apres le run"
        )


class TestClassificationDesErreurs(unittest.TestCase):
    """Transformer un message anglais en diagnostic exploitable.

    Les messages de `scp` changent entre versions : les comparer mot a
    mot produit un diagnostic faux des la premiere mise a jour du
    client. Seule la **cause** est extraite, et elle est stable.
    """

    def test_les_causes_courantes_sont_reconnues(self):
        cas = [
            (b"scp: /dst/f.dmp: write failed: No space left on device",
             "espace disque insuffisant"),
            (b"oracle@src: Permission denied", "permission refusee"),
            (b"scp: /src/f.dmp: No such file or directory", "absent sur l'hote source"),
            (b"subsystem request failed on channel 0", "scp-legacy"),
            (b"Host key verification failed.", "known_hosts"),
            (b"ssh: connect to host tgt port 22: Connection refused", "SSH refusee"),
            (b"ssh: connect to host tgt port 22: Connection timed out", "delai de connexion"),
            (b"ssh: Could not resolve hostname tgt", "non resolu"),
            (b"scp: invalid packet length", "scp-legacy"),
            (b"scp: protocol error (version 1)", "version scp/sftp"),
        ]
        for brut, attendu in cas:
            with self.subTest(cas=attendu):
                self.assertIn(attendu, _classify_error(brut))

    def test_un_message_sans_motif_est_conserve_integralement(self):
        """« Received message too long » n'a pas de motif : il passe brut.

        C'est le symptome le plus courant d'un **`sshd` ancien** face a un
        client OpenSSH 9, et sa cause n'est pas devinable a partir du
        texte. Tronquer ou resumer ferait perdre la seule chose que
        l'exploitant peut verifier par lui-meme : la version des deux
        bouts.
        """
        message = b"scp: Received message too long"
        self.assertIn(message.decode(), _classify_error(message))

    def test_un_message_inconnu_est_conserve_tel_quel(self):
        """Plutot que « echec », le texte d'origine est garde.

        Un message inconnu n'est pas encore un message sans valeur : il
        peut contenir exactement ce que la table ne prevoit pas. Le
        tronquer a blanc perdrait le diagnostic ; le garder permet a
        l'exploitant, et a la table, de s'en servir.
        """
        self.assertIn(
            "je ne sais pas quoi faire",
            _classify_error(b"scp: je ne sais pas quoi faire de ca"),
        )

    def test_un_message_absent_ne_produit_pas_de_diagnostic_vide(self):
        """Une chaine vide dans un rapport n'apprend rien.

        « echec sans message exploitable » vaut mieux : c'est
        explicitement un appel a chercher ailleurs, alors qu'une chaine
        vide se lit comme un trou de remplissage — donc comme une
        absence de probleme.
        """
        self.assertTrue(_classify_error(b"   \n  ").strip())

    def test_un_secret_present_dans_le_message_est_masque(self):
        """Le diagnostic part dans le rapport, donc dans un ticket.

        Le message d'origine est recopie pour le rendre reproductible, et
        c'est ce qui rend la recopie dangereuse : `scp` et `ssh` rappellent
        des parametres qu'ils ont recus, dont un `password=` dans
        certaines configurations. Le motif passe par `redact`, donc la
        garantie est **uniforme** avec le reste du projet, ou la
        redaction est un dernier rempart applique a toute sortie de
        commande.
        """
        diagnostic = _classify_error(
            b"scp: fatal: Unable to authenticate: (password=MonMotDePasse)"
        )
        self.assertNotIn("MonMotDePasse", diagnostic)

    def test_le_mot_cle_du_diagnostic_est_conserve(self):
        """Masquer ne doit pas rendre le diagnostic inexploitable.

        Le motif — « permission refusee » — reste present meme quand le
        message d'origine est masque. Sans lui il ne resterait qu'une
        chaine dont on ne sait pas quoi faire, ce qui est le meme
        symptome qu'un echec sans diagnostic.
        """
        diagnostic = _classify_error(
            b"scp: /dst/f.dmp: Permission denied (password=MonMotDePasse)"
        )
        self.assertIn("permission refusee", diagnostic)


class TestCompteRendu(unittest.TestCase):
    """Ce que le rapport doit dire, et rien de plus."""

    def test_le_compte_rendu_decrit_la_topologie(self):
        """`method` est la topologie, pas un detail technique.

        La meme cle disait `local` d'un cote et la premiere option SSH
        de l'autre. Deux natures sous le meme nom, dans le meme
        rapport : rien ne distinguait « le dump a transite par le
        serveur de saut » de « l'option ConnectTimeout vaut 30 ».
        """
        outcome = TransferOutcome(
            backend="scp-legacy", files=["f.dmp"], bytes_total=10,
            method="relais",
        )
        self.assertEqual(outcome.to_dict()["method"], "relais")
        self.assertEqual(outcome.to_dict()["backend"], "scp-legacy")

    def test_le_compte_rendu_ne_contient_que_des_champs_connus(self):
        """Un rapport versionne ne doit pas deborder de champ.

        Un lecteur — tableau de bord, script de supervision — ne peut
        pas anticiper une cle qu'il n'a jamais vue. Le test fige la
        forme exacte plutot que d'en accepter l'evolution silencieuse.
        """
        self.assertEqual(
            set(TransferOutcome(backend="scp").to_dict()),
            {"backend", "method", "files", "bytes_total", "duration_s"},
        )

    def test_la_liste_des_fichiers_est_copiee(self):
        """Un alias expose permettrait de muter le resultat apres coup.

        `to_dict` alimente l'etat, qui est relu a la construction du
        rapport. Une liste partagee permettrait a un appelant tardif
        d'ajouter un fichier « transfere » qui ne l'a pas ete.
        """
        outcome = TransferOutcome(backend="scp", files=["a.dmp"])
        rendu = outcome.to_dict()
        outcome.files.append("b.dmp")
        self.assertEqual(rendu["files"], ["a.dmp"])

    def test_la_duree_est_arrondie(self):
        """Une duree a douze decimales rend le rapport non reproductible.

        Deux executions de la meme duplication ne produiraient pas le
        meme document, et toute comparaison automatisee — un `diff` —
        serait faussee.
        """
        self.assertEqual(
            TransferOutcome(backend="scp", duration_s=1.23456789).to_dict()["duration_s"],
            1.235,
        )

    def test_les_tailles_sont_formatees_sans_la_locale(self):
        """`1000.0 Ko` en fr et `1000,0 Ko` selon `LC_ALL`.

        Le rapport doit etre identique sur tous les hotes. Le formatage
        se fait donc a la main, et le test verifie l'absence de
        virgule — un `str.replace(",", ".")` inverserait le probleme
        au lieu de le resoudre.
        """
        self.assertEqual(human_bytes(0), "0 o")
        self.assertEqual(human_bytes(1023), "1023 o")
        self.assertEqual(human_bytes(1024), "1.0 Ko")
        self.assertEqual(human_bytes(1536), "1.5 Ko")
        self.assertEqual(human_bytes(1024 ** 2), "1.0 Mo")
        self.assertEqual(human_bytes(1024 ** 3), "1.0 Go")
        self.assertEqual(human_bytes(1024 ** 4), "1.0 To")
        for valeur in (0, 1023, 1024, 1536, 1024 ** 2, 1024 ** 3, 1024 ** 4):
            self.assertNotIn(",", human_bytes(valeur))


class TestOrdreDesBackends(unittest.TestCase):
    """`AUTO_ORDER` n'est pas une preference, c'est une politique."""

    def test_rsync_est_essaye_en_premier(self):
        """C'est le seul mecanisme resumable, et le plus robuste."""
        self.assertEqual(AUTO_ORDER[0], "rsync")

    def test_scp_legacy_est_essaye_avant_scp(self):
        """Sur AIX, `scp` en mode SFTP echoue presque toujours.

        Le sous-systeme `sftp` manque a l'`sshd` d'AIX, et l'erreur
        (« subsystem request failed ») n'apparait qu'a l'execution.
        Sans cet ordre, chaque run perd le temps d'un essai qui ne peut
        pas aboutir — et l'exploitant conclut que le transfert est
        impossible, alors que `scp -O` reussit.
        """
        self.assertLess(AUTO_ORDER.index("scp-legacy"), AUTO_ORDER.index("scp"))

    def test_un_mode_force_ne_cherche_pas_de_repli(self):
        """Un choix explicite doit etre honore, meme s'il echoue.

        Revenir silencieusement sur un autre mecanisme ferait echouer le
        transfert la ou il est le plus utile — un gros dump, ou le seul
        remede est la reprise partielle de `rsync`. L'exploitant
        decouvrirait l'echec en production, et la configuration ne
        dirait rien de ce qui a reellement ete tente.
        """
        source = RunnerFaux(label="source", binaries=())
        cible = RunnerFaux(label="cible")
        be = backend(source=source, target=cible, mode="scp")
        with SansReseau(source, cible, echecs=("scp", "sftp", "rsync")):
            with self.assertRaises(TransferError) as contexte:
                be.resolve(src_dir="/d", dst_dir="/d")
        detail = str(contexte.exception.detail)
        self.assertIn("scp", detail)
        self.assertNotIn("rsync:", detail)

    def test_chaque_echec_est_consigne(self):
        """« scp a echoue » n'aide personne.

        « scp a echoue car le sous-systeme sftp est absent, scp-legacy a
        reussi » aide. Le detail porte donc **toutes** les tentatives,
        dans l'ordre, avec leur motif.
        """
        source = RunnerFaux(label="source", binaries=())
        cible = RunnerFaux(label="cible", binaries=())
        be = backend(source=source, target=cible, mode="auto")
        with SansReseau(source, cible, echecs=tuple(AUTO_ORDER)):
            with self.assertRaises(TransferError) as contexte:
                be.resolve(src_dir="/d", dst_dir="/d")
        detail = str(contexte.exception.detail)
        for nom in AUTO_ORDER:
            self.assertIn(nom, detail)

    def test_l_erreur_propose_un_remede_contextuel(self):
        """Le message doit dire quoi faire, pas seulement ce qui echoue.

        Le remede est ici connu et stable : sur AIX, le sous-systeme sftp
        est le premier reflexe. Le faire figurer dans l'erreur evite un
        aller-retour complet vers la documentation.
        """
        source = RunnerFaux(label="source", binaries=())
        cible = RunnerFaux(label="cible", binaries=())
        be = backend(source=source, target=cible, mode="auto")
        with SansReseau(source, cible, echecs=tuple(AUTO_ORDER)):
            with self.assertRaises(TransferError) as contexte:
                be.resolve(src_dir="/d", dst_dir="/d")
        self.assertIn("scp-legacy", str(contexte.exception.hint))


if __name__ == "__main__":
    unittest.main()
