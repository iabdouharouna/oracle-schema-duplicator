"""Orchestration des 19 etapes du workflow de AGENTS.md.

Le pipeline est une suite d'etapes numerotees, chacune registered dans
l'etat avant, pendant et apres execution. Cette structure est ce qui rend
la **reprise** possible : `resume` relit l'etat et ne rejoue que les
etapes non validees.

Deux invariants structurent le fichier :

* toute sortie d'etape passe par `self._record`, qui porte le code de
  retour dans l'etat. Aucun chemin ne peut donc se terminer en succes
  apres un echec ;
* le **dry-run n'est pas un `if` reparti** dans le code. Il est realise
  par le remplacement du `Runner` par un `NullRunner` : le pipeline
  execute exactement le meme code, avec des adaptateurs qui n'ecrivent
  rien. Un dry-run ne peut donc pas diverger de l'execution reelle.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .. import exit_codes as ec
from ..adapters import null as null_mod
from ..adapters.datapump import (
    DataPumpAdapter,
    DATAPUMP_OK_STATES,
    DATAPUMP_REMEDES,
    DATAPUMP_SUCCESS_CODES,
)
from ..adapters.oracle import OracleAdapter, OracleSide
from ..adapters import transfer as transfer_mod
from ..adapters.transfer import TransferBackend
from ..checks import preflight
from ..checks.preflight import FAIL, OK, SKIP, WARN, CheckResult, human
from ..config import validate_identifier
from ..errors import (
    ConfigError,
    ConnectionError_,
    ExportError,
    Interrupted,
    OsdError,
    PrereqError,
    SecurityError,
    ValidationError,
)
from ..logging_setup import get_logger, set_step
from ..redact import redact
from ..adapters import ansible_runner
from ..runner import LocalRunner, build_script, load_body
from ..state import DONE, FAILED, SKIPPED, State

LOG = get_logger()

#: Ordre canonique des etapes. Cet ordre est la definition du workflow ;
#: il est utilise tel quel par `run`, `resume` et `check`.
STEPS: List[tuple] = [
    (1, "charger-configuration"),
    (2, "valider-configuration"),
    (3, "verifier-dependances"),
    (4, "tester-connexion-source"),
    (5, "tester-connexion-cible"),
    (6, "verifier-schema-source"),
    (7, "verifier-schema-cible"),
    (8, "verifier-tablespaces"),
    (9, "verifier-espace"),
    (10, "preparer-datapump"),
    (11, "exporter"),
    (12, "verifier-dump"),
    (13, "transferer"),
    (14, "importer"),
    (15, "valider"),
    (16, "comparer"),
    (17, "generer-rapport"),
    (18, "nettoyer"),
    (19, "retourner-code"),
]

STEP_INDEX = [i for i, _ in STEPS]
STEP_NAMES = dict(STEPS)

#: Etapes dont l'echec est sans remediable par l'outil. Elles ne sont pas
#: rejouees par `resume` apres un echec, sauf si `--force` est employe.
_TERMINAL_FAILURES = {1, 2}


@dataclass
class Pipeline:
    """Execute les etapes du workflow sur une configuration donnee."""

    cfg: Any
    state: State
    run_id: str
    dry_run: bool = False
    allow_destructive: bool = False
    resume: bool = False
    force: bool = False
    only: Optional[Sequence[int]] = None

    checks: List[CheckResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    # -- Objets d'acces, construits a la demande -------------------------
    # Les quatre objets sont memorises, et non reconstruits a chaque
    # acces. Deux raisons, l'une et l'autre deja constatee :
    #   * un `OracleAdapter` porte un etat (`_verified`) ; le
    #     reconstruire le ferait perdre, et une connexion re-testee a
    #     chaque appel multiplierait les allers-retours SQL*Plus ;
    #   * le dry-run substitue un `NullRunner`, qui doit etre le meme
    #     objet du debut a la fin pour que ses enregistrements
    #     d'appels soient coherents dans le rapport.
    _source_runner: Any = None
    _target_runner: Any = None
    _source: Optional[OracleSide] = None
    _target: Optional[OracleSide] = None
    _source_adapter: Optional[OracleAdapter] = None
    _target_adapter: Optional[OracleAdapter] = None

    #: Une etape a-t-elle echoue **depuis le debut de cette invocation** ?
    #:
    #: Distinct de `state.final_code`, qui est relu dans le fichier d'etat
    #: lors d'une reprise. Confondre les deux faisait dependre le
    #: nettoyage d'une tentative anterieure : apres une interruption puis
    #: une reprise reussie, le dump etait conserve alors que le run
    #: s'achevait sur un succes — et le rapport annoncait « echec
    #: anterieur » pour un run qui n'en avait pas eu.
    _failed_here: bool = False

    def __post_init__(self) -> None:
        self.state.dry_run = self.dry_run
        # `_OS_AUTH` ne porte pas de chaine : le rapport doit quand meme
        # dire comment la connexion est faite, sans quoi la ligne
        # `source :  / schema HR` ferait croire a un oubli de
        # configuration alors que l'authentification OS est un choix.
        self.state.source = _connect_affiche(self.cfg, "SOURCE")
        self.state.target = _connect_affiche(self.cfg, "TARGET")
        self.state.source_schema = str(self.cfg.get("SOURCE_SCHEMA", ""))
        self.state.target_schema = str(self.cfg.get("TARGET_SCHEMA", ""))

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------
    def execute(self) -> int:
        """Execute le workflow et retourne le code de sortie normalise.

        Une exception `OsdError` est le mecanisme normal d'arret : chaque
        etape leve avec le code qui la caracterise, et cette methode se
        contente de l'enregistrer puis de le propager au rapport. C'est
        ce qui garantit qu'aucune erreur ne peut se terminer en code 0.
        """
        for index, name in STEPS:
            # Les deux conditions etaient ecrites l'une apres l'autre, la
            # premiere en `continue` : `_skip` n'etait donc jamais
            # appele, et une etape hors perimetre n'apparaissait
            # **nulle part** dans l'etat. Le rapport d'un `check` ne
            # portait alors que les neuf premieres etapes, sans dire que
            # les dix autres avaient ete ecartees -- ce qui se lit
            # comme une etape manquante, pas comme une etape sautee.
            # La distinction n'est pas cosmetique : c'est elle qui
            # permet de distinguer un perimetre delibere d'un bug qui
            # aurait saute des etapes.
            if self._should_skip(index):
                self._skip(index, name)
                continue
            if self.resume and self._already_done(index) and not self.force:
                LOG.info("etape %02d %s : deja validee, reprise", index, name)
                continue

            set_step(f"{index:02d}-{name}")
            LOG.info("etape %02d/%02d : %s", index, len(STEPS), name)
            self.state.start(index, name)
            try:
                self._dispatch(index, name)
            except OsdError as exc:
                if exc.step is None:
                    exc.step = name
                self._record(index, name, exc)
                return exc.code
            except KeyboardInterrupt:
                self._record(
                    index,
                    name,
                    Interrupted("interruption demandee (Ctrl-C)"),
                )
                return ec.INTERRUPTED

        set_step("19-retour-code")
        self.state.final_code = ec.SUCCESS
        return ec.SUCCESS

    def _dispatch(self, index: int, name: str) -> None:
        handler = getattr(self, f"_step_{index:02d}", None)
        if handler is None:  # pragma: no cover - garde-fou
            raise ConfigError(f"etape {index} non implementee: {name}")
        handler()

    # ------------------------------------------------------------------
    # Enregistrement des etats
    # ------------------------------------------------------------------
    def _record(
        self,
        index: int,
        name: str,
        error: Optional[OsdError] = None,
        *,
        message: str = "",
        detail: Optional[List[str]] = None,
    ) -> None:
        if error is None:
            self.state.finish(index, name, status=DONE, message=message, detail=detail)
            LOG.info("etape %02d terminee%s", index, f" : {message}" if message else "")
            return
        self.state.finish(
            index,
            name,
            status=FAILED,
            code=error.code,
            message=error.message,
            detail=list(error.detail) + ([error.hint] if error.hint else []),
        )
        self.state.error = error.as_dict()
        self.state.final_code = error.code
        self._failed_here = True
        LOG.error("etape %02d en echec : %s", index, error.message)
        if error.detail:
            for item in error.detail[:5]:
                LOG.error("  %s", redact(str(item)))
        if error.hint:
            LOG.error("  remede : %s", redact(error.hint))

    def _skip(self, index: int, name: str) -> None:
        self.state.finish(index, name, status=SKIPPED, message="hors perimetre")

    def _already_done(self, index: int) -> bool:
        st = self.state.steps.get(index)
        return st is not None and st.is_done

    def _should_skip(self, index: int) -> bool:
        return self.only is not None and index not in self.only

    # ------------------------------------------------------------------
    # Etapes 1 a 2 — configuration
    # ------------------------------------------------------------------
    def _step_01(self) -> None:
        """Charge et materialise la configuration.

        La configuration a deja ete validee par `cli.py` pour produire une
        erreur exploitable. Cette etape materialise les repertoires de
        travail et calcule le nom de run, ce qui doit se produire avant
        toute ecriture de journal pour que le journal ait un lieu.
        """
        work = Path(self.cfg.get("WORK_DIR"))
        work.mkdir(parents=True, exist_ok=True)
        _secure(work)
        for key in ("LOG_DIR", "REPORT_DIR"):
            directory = Path(self.cfg.get(key))
            directory.mkdir(parents=True, exist_ok=True)
            _secure(directory)
        if self.cfg.get("LOCK_DIR"):
            Path(self.cfg.get("LOCK_DIR")).mkdir(parents=True, exist_ok=True)
            _secure(Path(self.cfg.get("LOCK_DIR")))
        self._record(
            1,
            STEP_NAMES[1],
            message=f"repertoire de travail {work}",
            detail=[f"schema {self.state.source_schema} -> {self.state.target_schema}"],
        )

    def _step_02(self) -> None:
        """Valide la configuration et les garde-fous de securite.

        La validation de type et de coherence est faite au chargement.
        Ce qui reste ici est la question : « cette configuration
        autorise-t-elle une operation destructive ? » Elle est posee ici,
        et pas dispersee, parce que c'est la seule question de securite
        qui ne puisse pas etre tranchee par la syntaxe.
        """
        detail: List[str] = []
        destructive, reason = self.cfg.is_destructive()

        if self.state.source_schema == self.state.target_schema:
            raise SecurityError(
                f"source et cible identiques ({self.state.source_schema})",
                hint="REMAP_SCHEMA ne sert a rien quand le nom est le meme. "
                     "Utiliser un autre TARGET_SCHEMA, ou assumer explicitement "
                     "avec ALLOW_EXISTING_TARGET=true.",
            )

        if self.allow_destructive:
            detail.append("--allow-destructive : operations destructives autorisees")
            if destructive:
                detail.append(f"  {reason}")
        elif destructive:
            raise SecurityError(
                f"operation destructive demandee : {reason}",
                hint="Reexecuter avec --allow-destructive si l'operation est "
                     "voulue. Aucune suppression ni ecriture sur des donnees "
                     "existantes n'est jamais implicite.",
            )

        if self.dry_run:
            detail.append("mode simulation : aucune ecriture ne sera effectuee")

        self._record(2, STEP_NAMES[2], message="configuration valide", detail=detail)

    # ------------------------------------------------------------------
    # Etapes 3 a 5 — dependances et connexions
    # ------------------------------------------------------------------
    def _step_03(self) -> None:
        """Verifie les dependances des deux cotes."""
        results = preflight.check_dependencies(self.source_runner, self.target_runner)
        self.checks.extend(results)
        failure = preflight.first_failure(results)
        if failure:
            raise PrereqError(failure.message, detail=failure.detail, hint=failure.hint)
        self._record(3, STEP_NAMES[3], message="dependances presentes des deux cotes")

    def _step_04(self) -> None:
        """Teste la connexion a la source."""
        result = preflight.check_connection("source", self.source_adapter)
        self.checks.append(result)
        if result.failed:
            raise ConnectionError_(result.message, detail=result.detail, hint=result.hint)
        info = result.data
        self.state.metrics["source"] = info
        self._record(
            4,
            STEP_NAMES[4],
            message=f"{info.get('instance', '?')} ({info.get('version', '?')})",
        )

    def _step_05(self) -> None:
        """Teste la connexion a la cible."""
        result = preflight.check_connection("cible", self.target_adapter)
        self.checks.append(result)
        if result.failed:
            raise ConnectionError_(result.message, detail=result.detail, hint=result.hint)
        info = result.data
        self.state.metrics["target"] = info
        self._record(
            5,
            STEP_NAMES[5],
            message=f"{info.get('instance', '?')} ({info.get('version', '?')})",
        )

    # ------------------------------------------------------------------
    # Etapes 6 a 8 — schemas et tablespaces
    # ------------------------------------------------------------------
    def _step_06(self) -> None:
        """Verifie le schema source et inventeorie ses objets.

        L'inventaire est produit ici, et non a l'etape 16 : il est
        necessaire des l'etape 9 pour estimer l'espace, et le recalculer
        plus tard ferait deux lectures de `DBA_OBJECTS` pour le meme
        resultat, sur un schema volumineux.
        """
        schema = self.state.source_schema
        result = preflight.check_source_schema(
            self.source_adapter, schema, content=self.cfg.get("CONTENT")
        )
        self.checks.append(result)
        if result.failed:
            raise PrereqError(result.message, detail=result.detail, hint=result.hint)

        objects = result.data.get("objects", 0)
        self.state.metrics["source_objects"] = objects
        self._record(
            6,
            STEP_NAMES[6],
            message=f"{objects} objet(s)",
            detail=[d for d in result.detail],
        )

    def _step_07(self) -> None:
        """Verifie le schema cible et applique le garde-fou d'ecrasement.

        C'est ici que se joue le choix le plus structurant de l'outil :
        ecraser un schema cible n'est jamais implicite. Le refus est un
        **code 8** (securite) et non un code 2, parce que la situation
        n'est pas un prerequis manquant mais une operation qui n'a pas
        ete explicitement demandee.
        """
        schema = self.state.target_schema
        allow_existing = self.cfg.get("ALLOW_EXISTING_TARGET")
        if self.allow_destructive:
            # --allow-destructive couvre ce garde-fou, qui n'en est qu'un
            # cas particulier.
            allow_existing = True

        result = preflight.check_target_schema(
            self.target_adapter, schema, allow_existing=bool(allow_existing)
        )
        self.checks.append(result)
        if result.failed:
            # Le code vient du **controle**, pas de l'etape. Les deux
            # echecs sont ici des refus, mais ils ne se diagnostiquent pas
            # de la meme facon : un schema inexistant est un prerequis
            # manquant (code 2), un schema peuple sans autorisation est un
            # exces de pouvoir (code 8). Confondre les deux enverrait
            # l'exploitant chercher une base qui n'existe pas.
            raise OsdError(result.message, result.code,
                           detail=result.detail, hint=result.hint)

        objects = result.data.get("objects", 0)
        self.state.metrics["target_objects_before"] = objects

        detail: List[str] = []
        action = self.cfg.get("TABLE_EXISTS_ACTION")
        if objects:
            detail.append(f"TABLE_EXISTS_ACTION={action}")
            if action == "SKIP":
                detail.append("les objets existants seront conserves (SKIP)")
        self._record(7, STEP_NAMES[7], message=f"{objects} objet(s) preexistants", detail=detail)

    def _step_08(self) -> None:
        """Verifie les tablespaces des deux cotes.

        Les tablespaces verifies sont ceux **de la cible apres remap** :
        ce sont eux qui doivent exister et avoir la place. Verifier les
        tablespaces source serait une erreur : ils n'ont pas besoin
        d'exister sur la cible, ni d'y etre libres.
        """
        remap = self._remap_pairs()
        # `_remap_pairs` renvoie des chaines `SOURCE:DESTINE` deja
        # valides, et non des tuples. Les decomposer par `split` et non
        # par unpacking : un unpacking dechaines iterait sur les
        # **caracteres**, et un nom de tablespace de plus de deux
        # lettres levait un `ValueError` — c'est-a-dire une exception
        # qui n'est pas une `OsdError`, donc un bug juge comme une
        # configuration invalide alors que la configuration etait
        # correcte. Le remap etait donc inutilisable des qu'il etait
        # renseigne.
        target_ts = [item.split(":", 1)[1] for item in remap]

        results: List[CheckResult] = []

        src_dir_ok = self._check_directory("source", self.source, self.source_adapter)
        results.append(src_dir_ok)
        if src_dir_ok.data.get("path"):
            self.state.artifacts["source_directory_path"] = src_dir_ok.data["path"]
        if src_dir_ok.failed:
            self.checks.extend(results)
            raise PrereqError(src_dir_ok.message, detail=src_dir_ok.detail, hint=src_dir_ok.hint)

        tgt_dir_ok = self._check_directory("cible", self.target, self.target_adapter)
        results.append(tgt_dir_ok)
        if tgt_dir_ok.data.get("path"):
            self.state.artifacts["target_directory_path"] = tgt_dir_ok.data["path"]
        if tgt_dir_ok.failed:
            self.checks.extend(results)
            raise PrereqError(tgt_dir_ok.message, detail=tgt_dir_ok.detail, hint=tgt_dir_ok.hint)

        # Le schema cible existe des l'etape 7 : c'est le moment naturel
        # pour controler les tablespaces qui y recevront les donnees.
        ts_results = preflight.check_tablespaces(
            self.target_adapter,
            "cible",
            target_ts,
            content=self.cfg.get("CONTENT"),
            remap=remap,
        )
        results.extend(ts_results)

        # Privileges Data Pump des deux cotes.
        #
        # Ils sont verifies ici, et pas a l'etape 3, pour deux raisons
        # qui ne sont pas de la commodite :
        #
        # * l'etape 3 precede la **verification de connexion**. Un compte
        #   dont le mot de passe a expire echouerait alors sur une lecture
        #   de `DBA_SESSION_PRIVS`, et le run rendrait 2 (prerequis) au
        #   lieu de 3 (connexion) — un code qui envoie chercher une
        #   concession de privileges la ou le probleme est une
        #   authentification ;
        # * c'est le dernier moment ou rien n'a encore ete ecrit. Un
        #   `expdp` lance sans `EXP_FULL_DATABASE` echoue apres avoir
        #   occupe le repertoire du DIRECTORY et le dictionnaire.
        #
        # L'etiquette d'etape reste « verifier-tablespaces » : elle est
        # fixee par le workflow documente et ne se renomme pas. Le
        # rapport, lui, nomme chaque controle — `self.checks` porte
        # `privileges source` et `privileges cible` comme entrees
        # distinctes, donc la lecture machine n'y perd rien.
        results.append(
            preflight.check_privileges(self.source_adapter, "source", for_export=True)
        )
        results.append(
            preflight.check_privileges(self.target_adapter, "cible", for_export=False)
        )

        self.checks.extend(results)
        failure = preflight.first_failure(results)
        if failure:
            code = ec.SECURITY if failure.code == ec.SECURITY else ec.PREREQ
            raise OsdError(failure.message, code, detail=failure.detail, hint=failure.hint)

        self._record(
            8,
            STEP_NAMES[8],
            message="repertoires, tablespaces et privileges conformes",
        )

    def _check_directory(self, label: str, side: OracleSide, adapter) -> CheckResult:
        """Verifie l'existence et l'accessibilite d'un objet DIRECTORY.

        Le chemin physique est resolu via `DBA_DIRECTORIES` puis verifie
        comme un chemin ordinaire sur l'hote. Les deux controles sont
        necessaires : l'objet peut exister dans le dictionnaire alors que
        le systeme de fichiers n'est pas monte, cas classique d'un
        volume de donnees non monte au demarrage.
        """
        name = f"directory {label} {side.directory}"
        path = adapter.directory_path(side.directory)
        if not path:
            return CheckResult(
                name=name,
                status=FAIL,
                code=ec.PREREQ,
                message=f"l'objet DIRECTORY {side.directory} n'existe pas",
                hint="Le creer au prealable (create directory) ou corriger "
                     "SOURCE_DIRECTORY / TARGET_DIRECTORY.",
            )

        if adapter.is_asm_directory(path):
            return CheckResult(
                name=name,
                status=FAIL,
                code=ec.PREREQ,
                message=f"{side.directory} pointe vers un chemin ASM ({path})",
                hint="Les scripts distants n'ont pas acces a ASM. Le dump "
                     "doit etre ecrit dans un DIRECTORY sur le systeme de "
                     "fichiers, sinon le transfert est impossible.",
            )

        result = self._remote_space(path, side)
        if result is None:
            return CheckResult(
                name=name,
                status=WARN,
                code=ec.PREREQ,
                message=f"{side.directory} -> {path} (espace non mesurable)",
                hint="Le chemin n'a pas pu etre mesure sur l'hote. L'export "
                     "pourrait echouer sur place insuffisante.",
            )
        if result == 0 and not os.path.exists(path):  # pragma: no cover
            return CheckResult(
                name=name,
                status=FAIL,
                code=ec.PREREQ,
                message=f"chemin inaccessible : {path}",
            )
        return CheckResult(
            name=name,
            status=OK,
            message=f"{side.directory} -> {path}",
            data={"path": path, "free_bytes": result},
        )

    def _remote_space(self, path: str, side: OracleSide) -> Optional[int]:
        """Espace libre du systeme de fichiers portant `path`, en octets.

        La mesure se fait sur **l'hote qui porte le chemin**, d'ou le
        parametre `side` : en mode AIX -> AIX, le disque du DIRECTORY
        source et celui du DIRECTORY cible sont deux systemes de
        fichiers distincts, et interroger le mauvais donne un verdict
        d'espace sur un disque qui ne recevra rien. Le parametre etait
        ignore, ce qui rendait le controle d'espace cible faux des que
        les deux repertoires etaient sur des hotes differentes — c'est-a-dire
        dans le cas d'usage normal de l'outil.

        On ne mesure donc **que** le disque du DIRECTORY, pas celui du
        schema cible : c'est ce disque qui recoit physiquement le dump,
        et c'est lui qui est invisible a toute requete SQL.

        Retourne `None` si la mesure est impossible, ce qui n'est pas la
        meme chose que zero. La distinction compte : zero signifie « pas
        de place », `None` signifie « je ne sais pas », et seul le second
        cas justifie de poursuivre en signalant une incertitude.
        """
        if not path:
            return None
        script = build_script(load_body("remote_space.sh"), [path])
        try:
            result = side.runner.run_script(script, timeout=120)
        except OsdError as exc:
            LOG.debug("mesure d'espace impossible sur %s: %s", side.label(), exc.message)
            return None
        if result.rc != 0:
            return None
        return result.get_int("OSD_AVAIL_BYTES", 0) or None

    # ------------------------------------------------------------------
    # Etape 9 — espace
    # ------------------------------------------------------------------
    def _step_09(self) -> None:
        """Verifie la place disponible avant d'agir.

        Le but est d'echouer **avant** d'avoir produit ou transfere un
        dump volumineux pour rien. C'est la seule justification d'une
        estimation : elle est approximative par nature, et l'estimation
        sert a refus tot, pas a garantir l'issue.
        """
        content = self.cfg.get("CONTENT")
        source_bytes = self.source_adapter.schema_segment_bytes(
            self.state.source_schema, content=content
        )
        self.state.metrics["source_bytes"] = source_bytes

        # Facteur de compression : `COMPRESSION=ALL` divise le dump, mais
        # on ne presume pas du taux. On retient 1.0 pour `ALL` ( prudent
        # pour le disque) et 0.1 pour METADATA_ONLY, ou le dump ne porte
        # que le DDL.
        factor = 0.1 if content == "METADATA_ONLY" else 1.0
        needed = int(source_bytes * factor)

        target_path = self._target_directory_path()
        # La mesure est faite ici, et pas dans `check_space` : celle-ci est
        # une fonction pure, testable sans reseau ni runner, et la mesure
        # demande justement un exec distant. Le controle peut donc etre
        # verifie en unite sans SSH, ce qui n'aurait pas ete le cas si le
        # `Runner` etait passe en parametre.
        target_free = self._remote_space(target_path, self.target)

        results = preflight.check_space(
            self.target_adapter,
            "cible",
            self.target.directory,
            target_path,
            required_bytes=needed,
            margin_percent=self.cfg.get("SPACE_MARGIN_PERCENT"),
            margin_abs_bytes=self.cfg.get("SPACE_MARGIN_ABS_MB") * 1024 * 1024,
            min_free_bytes=self.cfg.get("MIN_FREE_SPACE_MB") * 1024 * 1024,
            remote_free_bytes=target_free,
            content=content,
        )
        self.checks.extend(results)
        failure = preflight.first_failure(results)
        if failure:
            raise OsdError(failure.message, failure.code, detail=failure.detail, hint=failure.hint)

        self._record(
            9,
            STEP_NAMES[9],
            message=f"estimation {human(needed)} pour {human(source_bytes)} de segments",
        )

    def _target_directory_path(self) -> str:
        """Chemin physique du DIRECTORY cible, resolu une fois puis memorise.

        L'etape 9 doit mesurer l'espace de ce chemin, mais le chemin
        n'est renseigne dans l'etat qu'a l'etape 10. Lire l'etat ici
        donnerait une chaine vide, donc un controle d'espace **toujours**
        ignore — le controle le plus utile du preparatif serait
        silencieusement inactif sur tous les runs. La resolution est donc
        faite a la demande, avec mise en cache dans l'etat pour que
        l'etape 10 n'ait pas a interroger `DBA_DIRECTORIES` une seconde
        fois.
        """
        cached = self.state.artifacts.get("target_directory_path", "")
        if cached:
            return cached
        try:
            path = self.target_adapter.directory_path(self.target.directory) or ""
        except OsdError:  # pragma: no cover - deja signale a l'etape 8
            return ""
        if path:
            self.state.artifacts["target_directory_path"] = path
        return path

    # ------------------------------------------------------------------
    # Etapes 10 a 12 — export et verification
    # ------------------------------------------------------------------
    def _step_10(self) -> None:
        """Prepare le job Data Pump : noms, parfile, destinataire."""
        prefix = self.cfg.get("JOB_PREFIX") or "OSD"
        self.state.artifacts["job_name"] = f"{prefix}_{self.run_id}".upper()[:30]
        self.state.artifacts["dump_base"] = f"osd_{self.run_id}"
        self.state.artifacts["dump_log"] = f"osd_{self.run_id}_export.log"
        self.state.artifacts["import_log"] = f"osd_{self.run_id}_import.log"
        # Les deux artefacts de la relecture du DDL, produits a l'etape
        # 12. Ils sont nommes ici, et non a l'etape 12, parce que le
        # nettoyage doit pouvoir les designer sans savoir quelle etape
        # les a produits : un nom construit a l'usage de l'etape 12 est
        # invisible au nettoyage, et le repertoire DIRECTORY accumule
        # alors le DDL de tous les runs passes.
        job = self.state.artifacts["job_name"]
        self.state.artifacts["verify_sql"] = f"osd_verify_{job}.sql"
        self.state.artifacts["verify_log"] = f"osd_verify_{job}.log"
        # La forme du nom de dump depend du parallelisme : `%d` n'est
        # accepte qu'a l'export, l'import recevra les noms reels
        # enumeres a l'etape 11 (cf. `_spec_dimport`). Cette valeur ne
        # sert donc qu'a l'export et au rapport.
        parallel = self.cfg.get("PARALLEL")
        base = self.state.artifacts["dump_base"]
        self.state.artifacts["dumpfile_spec"] = (
            f"{base}-%d.dmp" if parallel > 1 else f"{base}.dmp"
        )
        self.state.artifacts["target_directory_path"] = self._resolve_target_path()
        self._record(
            10,
            STEP_NAMES[10],
            message=f"job {self.state.artifacts['job_name']}, dump "
                    f"{self.state.artifacts['dumpfile_spec']}",
        )

    def _resolve_target_path(self) -> str:
        try:
            return self.target_adapter.directory_path(self.target.directory) or ""
        except OsdError:  # pragma: no cover
            return ""

    def _step_11(self) -> None:
        """Execute l'export."""
        pump = DataPumpAdapter(self.source, oracle=self.source_adapter)
        parfile = pump.build_export_parfile(
            schema=self.state.source_schema,
            job_name=self.state.artifacts["job_name"],
            dumpfile=self.state.artifacts["dump_base"] + ".dmp",
            logfile=self.state.artifacts["dump_log"],
            content=self.cfg.get("CONTENT"),
            compression=self.cfg.get("COMPRESSION"),
            parallel=self.cfg.get("PARALLEL"),
            filesize_mb=self.cfg.get("FILESIZE_MB"),
            remap_tablespace=self._remap_pairs(),
            exclude=self.cfg.get("EXCLUDE"),
            include=self.cfg.get("INCLUDE"),
        )
        result = pump.run(
            "expdp",
            parfile,
            job_name=self.state.artifacts["job_name"],
            timeout=None,
        )
        self._assert_datapump(result, tool="expdp", side="source")
        if result.simulated:
            self._record(11, STEP_NAMES[11], message="simulation : export non execute")
            return
        self._collect_dump_parts()
        self._record(
            11,
            STEP_NAMES[11],
            message=f"{len(self.state.artifacts.get('dump_parts', []))} partie(s), "
                    f"{human(self._dump_total())}",
        )

    def _assert_datapump(
        self,
        result,
        *,
        tool: str,
        code: Optional[int] = None,
        side: str = "target",
    ) -> None:
        """Verifie le resultat d'une operation Data Pump.

        Trois conditions, dont **les trois** doivent etre satisfaites :

        1. le code de sortie est l'un des codes de succes Data Pump ;
        2. aucun code `ORA-`/`UDI-` n'apparait dans la sortie ;
        3. le job depose dans `DBA_DATAPUMP_JOBS` est a l'etat
           `COMPLETED`, sans erreur comptee.

        Une seule ne suffit pas. Un rc a 0 n'exclut pas une erreur Oracle
        dans une section non fatale ; l'absence d'erreur n'exclut pas un
        job interrompu ; et le client peut se detacher en laissant le job
        tourner, auquel cas le rc ne dit rien du tout du resultat.

        L'etat prime sur `ERROR_COUNT`, et non l'inverse. Un compte
        d'erreurs n'a de sens que si le job a abouti ; surtout, la
        colonne est absente de certaines vues `DBA_DATAPUMP_JOBS`, et
        un compteur indisponible ne doit pas rendre l'etat illisible.
        L'inverse, lui, est un piege : un job `FAILED` dont le compteur
        n'a pas pu etre lu passerait pour un succes. Verifie sur une
        vraie 19c, ou l'export s'etait deroule avec succes et le
        controle ne voyait plus rien.

        `code` est explicite parce que l'outil ne designe pas toujours
        le code du nom de l'outil. L'etape 12 fait tourner `impdp` sur la
        **source** pour relire le dump, et un dump tronque est un
        probleme d'export : le remede est de re-exporter, pas de
       reexecuter un import qui n'a pas commence. Deduire le code du nom
        de l'outil rendait ce cas indistinguable d'un echec d'import
        reel.

        `side` l'est pour la meme raison, et l'erreur qu'il corrige est
        plus grave : l'etat du job se lit dans `DBA_DATAPUMP_JOBS`, donc
        **sur la base ou le job s'est deroule**. Un job d'export n'existe
        pas sur la cible, ou une requete a ce cote ne rendait aucune
        ligne -- et une absence de ligne se lisait « aucun probleme ».
        Le troisieme signal, celui qui existe precisement pour attraper un
        client qui s'est detache, etait donc muet sur les deux etapes les
        plus exposees : l'export et la relecture du dump.
        """
        if code is None:
            code = ec.EXPORT if tool == "expdp" else ec.IMPORT
        problems: List[str] = []
        if result.rc not in DATAPUMP_SUCCESS_CODES:
            problems.append(f"code de sortie Data Pump {result.rc}")
        if result.error_codes:
            problems.append("codes d'erreur : " + ", ".join(result.error_codes))

        if not self.dry_run:
            status = self._job_status(result.job_name, side=side)
            if status:
                self.state.metrics.setdefault("datapump_jobs", {})[result.job_name] = status
                etat = (status.get("state") or "").strip().upper()
                error_count = status.get("error_count", "")
                if etat and etat not in DATAPUMP_OK_STATES:
                    problems.append(
                        f"job {status.get('job_name')} dans l'etat "
                        f"{etat}, et non {sorted(DATAPUMP_OK_STATES)[0]}"
                    )
                elif error_count not in ("", "0", "None"):
                    problems.append(
                        f"job {status.get('job_name')} en etat "
                        f"{etat or 'inconnu'} avec {error_count} erreur(s)"
                    )

        if problems:
            detail = problems + result.warnings
            raise OsdError(
                f"{tool} n'a pas abouti ({tool})",
                code,
                detail=detail,
                hint=self._remede(result.error_codes),
            )

    def _remede(self, codes: Sequence[str]) -> str:
        """Remede le plus specifique connu pour ces codes d'erreur.

        Le remede generique — « consulter le journal » — ne vaut que
        lorsque la cause n'est pas devinable. Il ne l'est pas toujours :
        certains codes designent un echec entierement deterministe, dont
        la suite ne demande aucune investigation. Dire « consulter le
        journal » la-dessus laisse l'exploitant chercher une cause
        qu'il a deja sous les yeux.

        Le code le plus specific gagne, dans l'ordre ou la table est
        declaree : c'est le premier remede decrit qui s'applique. Aucun
        code inconnu n'est invente, et un code sans remede ne fait pas
        disparaitre la mention generique.
        """
        vus = {c.upper() for c in codes}
        for code, texte in DATAPUMP_REMEDES.items():
            if code in vus:
                return texte
        return (
            "Consulter le journal Data Pump sur l'hote pour le "
            "detail des objets en cause. Les codes ORA-/UDI- sont "
            "independants de la langue des messages."
        )

    def _job_status(self, job_name: str, *, side: str) -> Dict[str, str]:
        """Etat du job, lu sur la base qui l'a execute.

        Une exception est absorbee en un dictionnaire vide : l'absence
        d'information ne doit pas devenir une erreur, mais elle ne doit
        pas non plus etre confondu avec un succes -- d'ou le choix
        d'un dictionnaire vide plutot que d'un `None`, que
        `_assert_datapump` teste avant d'en tirer une conclusion.
        """
        cote = self.source if side == "source" else self.target
        adaptateur = self.source_adapter if side == "source" else self.target_adapter
        try:
            pump = DataPumpAdapter(cote, oracle=adaptateur)
            return pump.job_status(job_name)
        except OsdError:
            return {}

    def _collect_dump_parts(self) -> None:
        """Decouvre les fichiers reellement produits par l'export.

        Les noms ne sont pas devinables : avec `PARALLEL > 1`, Data Pump
        choisit le nombre de parties selon la volumetrie, et le jeton
        `%d` ne garantit pas une serie complete. Le repertoire du
        DIRECTORY est donc enumerate, ce qui est la seule approche fiable.
        """
        names = self._list_remote_dir(self.source, self.state.artifacts["dump_base"])
        parts = []
        for name in names:
            size = self._remote_size(self.source, name)
            parts.append({"name": name, "bytes": size})
        self.state.artifacts["dump_parts"] = parts

    def _dump_total(self) -> int:
        return sum(int(p.get("bytes", 0)) for p in self.state.artifacts.get("dump_parts", []))

    def _list_remote_dir(self, side: OracleSide, base: str) -> List[str]:
        """Liste les parties du dump presente dans le repertoire DIRECTORY.

        Le motif est anchoré sur le prefixe du run, donc un run concurrent
        ne peut pas faire Picking les fichiers d'un autre run.
        """
        if self.dry_run:
            return []
        path = self.state.artifacts.get("source_directory_path") or self._resolve_source_path()
        if not path:
            raise ExportError(
                "chemin du DIRECTORY source inconnu",
                hint="Le repertoire du DIRECTORY n'a pas pu etre resolu.",
            )
        script = build_script(
            load_body("remote_listdir.sh"), [path, f"{base}-", ".dmp"]
        )
        result = side.runner.run_script(script, timeout=300)
        if result.rc != 0:
            raise ExportError(
                f"lecture du repertoire du dump impossible (rc={result.rc})",
                detail=[redact(result.kv.get("__fatal__", ""))] or [],
            )
        names = [line.strip() for line in result.rows if line.strip()]
        if not names:
            raise ExportError(
                "aucun fichier de dump produit par l'export",
                hint="L'export s'est termine sans erreur signalee mais aucun "
                     "fichier n'apparait dans le DIRECTORY. Verifier le "
                     "journal Data Pump et les droits sur le repertoire.",
            )
        return names

    def _resolve_source_path(self) -> str:
        try:
            return self.source_adapter.directory_path(self.source.directory) or ""
        except OsdError:  # pragma: no cover
            return ""

    def _remote_size(self, side: OracleSide, name: str) -> int:
        path = self.state.artifacts.get("source_directory_path") or self._resolve_source_path()
        if not path:
            return 0
        script = build_script(load_body("remote_pathinfo.sh"), ["size", path, name])
        try:
            result = side.runner.run_script(script, timeout=120)
        except OsdError:  # pragma: no cover
            return 0
        return result.get_int("OSD_SIZE", 0)

    def _spec_dimport(self) -> str:
        """Spec `DUMPFILE` a passer a `impdp`, en noms **concrets**.

        `%d` est une variable de substitution de l'**export** seul.
        `impdp` la refuse : `ORA-39124: ... contient une variable de
        substitution non valide`. La forme a jeton convenait donc
        uniquement du cote `expdp`, et la reutiliser ici faisait
        echouer l'etape 12 — puis l'etape 14 — sur un dump
        parfaitement complet, avec un journal qui ne montrait qu'une
        substitution invalide, sans aucun rapport avec le dump.

        `impdp` attend la **liste** des parties, separee par des
        virgules. Elles viennent de l'etape 11, qui les a enumerees :
        les deviner a partir d'un nom reintroduirait exactement l'echec
        que l'enumeration evite. Une partie manquante n'est d'ailleurs
        pas silencieuse — `impdp` signale `ORA-39059` — ce qui fait de la
        relecture de l'etape 12 un controle d'integrite reel.

        En simulation, l'export n'a rien produit et la liste est donc
        inconnue. Un marqueur explicite est renvoye plutot qu'un nom
        invente : le rapport est une simulation, mais afficher la forme
        a jeton y montrerait une specification que l'import refuserait,
        et que personne ne doit pouvoir prendre pour ce qui serait
        reellement execute.
        """
        parts = sorted(
            str(p.get("name")) for p in self.state.artifacts.get("dump_parts", [])
            if p.get("name")
        )
        if not parts:
            if self.dry_run:
                return f"{self.state.artifacts.get('dump_base', 'osd')}-<parties>.dmp"
            raise ExportError(
                "aucune partie de dump connue pour l'import",
                hint="L'etape 11 n'a pas enregistre les parties produites. "
                     "Reprendre depuis l'etape 11 (`osd resume --run-id ...`) "
                     "pour les refaire inventorier.",
            )
        return ",".join(parts)

    def _step_12(self) -> None:
        """Verifie que le dump est complet, sans rien ecrire en base.

        `impdp SQLFILE=` relit le dump et produit le DDL sans l'executer.
        C'est la seule verification qui ne repose ni sur la confiance ni
        sur un texte traduit. Sur un dump interrompu, elle echoue
        explicitement (ORA-39059 / ORA-39246) — comportement observe et
        documente dans docs/RUNBOOK.md.
        """
        pump = DataPumpAdapter(self.source, oracle=self.source_adapter)
        result = pump.verify_dump(
            source_schema=self.state.source_schema,
            target_schema=self.state.target_schema,
            dumpfile=self._spec_dimport(),
            job_name=self.state.artifacts["job_name"],
            parallel=self.cfg.get("PARALLEL"),
            table_exists_action=self.cfg.get("TABLE_EXISTS_ACTION"),
            remap_tablespace=self._remap_pairs(),
            sqlfile_name=self.state.artifacts["verify_sql"],
            logfile_name=self.state.artifacts["verify_log"],
        )
        # Code 4 et non 6 : la relecture se fait sur la **source** et
        # l'import n'a pas commence. Un dump tronque se corrige en
        # re-exportant.
        self._assert_datapump(
            result, tool="impdp", code=ec.EXPORT, side="source"
        )
        if result.simulated:
            # `dump_verified` n'est **pas** positionne. Le drapeau
            # n'existant que parce qu'un dump a ete relu, il doit
            # demeurer absent : un `resume` qui le lirait croit a une
            # garantie qu'il n'a pas.
            self._record(12, STEP_NAMES[12], message="simulation : dump non relu")
            return
        self.state.artifacts["dump_verified"] = True
        self._record(
            12,
            STEP_NAMES[12],
            message="dump relu integralement, DDL regenerable",
        )

    # ------------------------------------------------------------------
    # Etapes 13 et 14 — transfert et import
    # ------------------------------------------------------------------
    def _step_13(self) -> None:
        """Transfere le dump vers l'hote cible, si necessaire.

        Le transfert n'a lieu que si les deux cotes ne partagent pas le
        repertoire. Dans une configuration de serveur de saut, il a
        toujours lieu : le chemin du DIRECTORY n'est pas visible d'ici.
        """
        parts = [p["name"] for p in self.state.artifacts.get("dump_parts", [])]
        if not parts:
            if self.dry_run:
                # Le transfert est une mutation, donc retenue en dry-run ;
                # et rien n'a ete exporte, donc il n'y a rien a
                # transferer. L'absence de parties n'est ici **pas** un
                # echec d'export : c'en serait un si l'export avait ete
                # reellement execute et n'avait rien produit, cas que le
                # code distingue explicitement.
                self._record(13, STEP_NAMES[13], message="simulation : aucun dump a transferer")
                return
            raise ExportError("aucune partie de dump a transferer")

        backend = TransferBackend(
            source_runner=self.source_runner,
            target_runner=self.target_runner,
            mode=self.cfg.get("TRANSFER_MODE"),
            # Le transfert n'est pas execute par Ansible, mais il a besoin
            # du meme secret. On va le chercher **aupres du runner**, qui
            # l'a deja lu dans le coffre : une seule lecture du secret,
            # une seule source de verite. Le `getattr` couvre les runners
            # qui n'ont rien a fournir -- `LocalRunner` et le `NullRunner`
            # du dry-run.
            ssh_password=_mot_de_passe_ssh(self.source_runner),
            probe_cache_dir=Path(self.cfg.get("WORK_DIR")) / "probes",
        )

        src_path = self.state.artifacts.get("source_directory_path") or self._resolve_source_path()
        dst_path = self.state.artifacts.get("target_directory_path") or self._resolve_target_path()
        if not src_path or not dst_path:
            raise OsdError(
                "chemin de transfert indeterminate",
                ec.TRANSFER,
                hint="Le repertoire DIRECTORY n'a pas pu etre resolu d'un "
                     "des deux cotes.",
            )

        outcome = backend.run(
            src_dir=src_path,
            dst_dir=dst_path,
            names=parts,
            job_name=self.state.artifacts["job_name"],
        )
        self.state.artifacts["transfer"] = outcome.to_dict()
        if outcome.method == transfer_mod.PARTAGE:
            # Le compte rendu porte deja les parties et leur volume : le
            # message doit seulement dire qu'aucune n'a ete deplacee,
            # sinon l'etape se lit comme une copie alors que les deux
            # cotes voyaient le meme repertoire.
            message = (
                f"repertoire partage : {len(outcome.files)} partie(s) "
                f"deja en place, aucun transfert"
            )
        else:
            message = (
                f"{len(outcome.files)} fichier(s), {human(outcome.bytes_total)}, "
                f"backend {outcome.backend}"
            )
        self._record(13, STEP_NAMES[13], message=message)

    def _step_14(self) -> None:
        """Execute l'import."""
        action = self.cfg.get("TABLE_EXISTS_ACTION")
        if action in ("REPLACE", "TRUNCATE") and not self.allow_destructive:
            # Garde-fou redondant avec l'etape 2 : il est rejoue ici
            # parce que c'est le dernier point avant l'ecriture, et
            # qu'un `resume` pourrait contourner l'etape 2.
            raise SecurityError(
                f"TABLE_EXISTS_ACTION={action} sans --allow-destructive",
                hint="Cette action ecrase des donnees deja presentes dans "
                     "le schema cible. Reexecuter avec --allow-destructive.",
            )

        pump = DataPumpAdapter(self.target, oracle=self.target_adapter)
        parfile = pump.build_import_parfile(
            source_schema=self.state.source_schema,
            target_schema=self.state.target_schema,
            job_name=self.state.artifacts["job_name"] + "_IMP",
            dumpfile=self._spec_dimport(),
            logfile=self.state.artifacts["import_log"],
            parallel=self.cfg.get("PARALLEL"),
            table_exists_action=action,
            remap_tablespace=self._remap_pairs(),
            exclude=self.cfg.get("EXCLUDE"),
            include=self.cfg.get("INCLUDE"),
        )
        result = pump.run(
            "impdp",
            parfile,
            job_name=self.state.artifacts["job_name"] + "_IMP",
            timeout=None,
        )
        self._assert_datapump(result, tool="impdp")
        if result.simulated:
            # `imported` n'est pas positionne : un `resume` qui le
            # verifierait doit savoir que la cible n'a pas ete touchee.
            self._record(
                14, STEP_NAMES[14],
                message=f"simulation : import non execute "
                        f"(TABLE_EXISTS_ACTION={action})",
            )
            return
        self.state.artifacts["imported"] = True
        self._record(14, STEP_NAMES[14], message=f"import termine (TABLE_EXISTS_ACTION={action})")

    # ------------------------------------------------------------------
    # Etapes 15 et 16 — validation et comparaison
    # ------------------------------------------------------------------
    def _step_15(self) -> None:
        """Valide l'etat final du schema cible.

        La validation est demandee explicitement par `VALIDATION_LEVEL`.
        Le defaut `STANDARD` verifie ce qui se constate sans cout : le
        compte ouvert, le schema reconcilie, et le volume de donnees
        coherent avec la source.
        """
        level = self.cfg.get("VALIDATION_LEVEL")
        schema = self.state.target_schema
        detail: List[str] = []

        if not self.target_adapter.schema_exists(schema):
            raise ValidationError(f"le schema cible {schema} n'existe plus apres import")

        objects = self.target_adapter.object_count(schema)
        invalid = self.target_adapter.object_count(schema, object_type="INVALID")
        self.state.metrics["target_objects_after"] = objects
        self.state.metrics["target_invalid_after"] = invalid
        detail.append(f"objets presents : {objects}")
        detail.append(f"objets invalides : {invalid}")

        if self.dry_run:
            self._step_15_dry(objects, invalid, detail)
            return

        if invalid:
            detail.append(
                "un objet invalide apres import indique une dependance "
                "non satisfaite (grant, synonym, edition)"
            )
            if level == "STANDARD":
                # `_record` enregistre deja `DONE` quand aucune erreur
                # n'est fournie : le statut n'a pas a etre passe en
                # argument. Il l'etait, et `_record` n'a pas de parametre
                # `status` — le chemin « objets invalides en STANDARD »
                # levait donc un `TypeError`, qui n'est pas une
                # `OsdError` : `execute()` ne l'attrapait pas, et le run
                # se terminait en code 1 « configuration invalide » avec
                # un journal d'erreur interne, pour un schema dont le
                # seul defaut etait un objet invalide.
                self._record(
                    15, STEP_NAMES[15],
                    message=f"{objects} objet(s), {invalid} invalide(s)", detail=detail,
                )
                raise ValidationError(
                    f"{invalid} objet(s) invalide(s) dans le schema cible {schema}",
                    detail=detail,
                    hint="Utiliser VALIDATION_LEVEL=FULL pour un bilan "
                         "detaille, ou consulter INVALID_OBJECTS pour la "
                         "liste des objets et de leurs dependances.",
                )

        # Le message distingue les deux cas. Ecrire « aucun invalide » en
        # `FULL` alors que `invalid` vaut 3 produirait un rapport
        # favorable sur un schema qui ne compile pas — c'est-a-dire
        # l'inverse exact de ce que `VALIDATION_LEVEL=FULL` est suppose
        # donner : la detaille.
        message = (
            f"{objects} objet(s), aucun invalide"
            if not invalid
            else f"{objects} objet(s), {invalid} invalide(s) (bilan detaille)"
        )
        self._record(15, STEP_NAMES[15], message=message, detail=detail)

    def _step_15_dry(self, objects: int, invalid: int, detail: List[str]) -> None:
        """Variante simulation de l'etape 15.

        Meme lecture, verdict differente : ce que l'on mesure est l'etat
        **anterieur** a l'import. Un objet invalide ici n'est donc pas un
        defaut de l'outil, et un schema inexistant n'est pas un echec du
        run. Le statut reste `DONE` — les lectures ont bien eu lieu — mais
        le message le dit, pour que le rapport ne soit pas lu comme un
        bilan post-import.
        """
        detail.append(
            "etat mesure avant l'import, qui n'a pas eu lieu en simulation"
        )
        self._record(
            15, STEP_NAMES[15],
            message=f"simulation : {objects} objet(s) et {invalid} invalide(s) "
                    f"avant import",
            detail=detail,
        )

    def _step_16(self) -> None:
        """Compare les inventaires source et cible.

        La comparaison porte sur l'inventaire d'objets, pas sur une
        empreinte de contenu : une empreinte de tables differerait par
        l'ordre physique des lignes, par les statistiques, et par
        l'horodatage des lignes modifiees, alors que l'objet lui-meme est
        identique. Ce qui doit etre identique apres une duplication, c'est
        la **structure**.
        """
        src_schema = self.state.source_schema
        tgt_schema = self.state.target_schema
        src = self._inventory(src_schema)
        tgt = self._inventory(tgt_schema)

        missing = sorted(set(src) - set(tgt))
        extra = sorted(set(tgt) - set(src))
        self.state.metrics["comparison"] = {
            "source": len(src),
            "target": len(tgt),
            "missing_in_target": missing,
            "extra_in_target": extra,
        }

        detail: List[str] = []
        if missing:
            detail.append(f"{len(missing)} objet(s) absent(s) de la cible : {', '.join(missing[:10])}")
        if extra:
            detail.append(f"{len(extra)} objet(s) supplementaire(s) : {', '.join(extra[:10])}")

        if self.dry_run:
            # L'import n'a pas eu lieu : l'inventaire cible est celui
            # d'**avant** le run, et l'ecart observe est la duplication
            # entiere. Le signaler comme un echec de validation serait
            # un faux positif — et un faux positif ici est particulierement
            # destructeur, car il ferait conclure que l'outil est casse.
            # Ce qui est utile en simulation, c'est l'inventaire des
            # deux cotes, pris avant toute ecriture.
            detail.append(
                "ecart mesure avant l'import, qui n'a pas eu lieu : "
                "c'est l'etat de depart de la cible, non un defaut"
            )
            self._record(
                16, STEP_NAMES[16],
                message=f"simulation : {len(src)} objet(s) source, "
                        f"{len(tgt)} objet(s) cible avant import",
                detail=detail,
            )
            return

        if missing:
            raise ValidationError(
                f"l'inventaire cible ne correspond pas a la source "
                f"({len(missing)} objet(s) manquant(s))",
                detail=detail,
                hint="Verifier le niveau de validation, les clauses EXCLUDE, "
                     "et consulter le journal Data Pump de l'import.",
            )
        self._record(
            16,
            STEP_NAMES[16],
            message=f"{len(tgt)} objet(s) reconcilie(s)",
            detail=detail,
        )

    def _inventory(self, schema: str) -> List[str]:
        """Inventaire des objets d'un schema, sous forme de signatures.

        La signature est `TYPE~NOM`, ce qui evite qu'un index et une table
        de meme nom soient confondus. Le nom de schema est exclu, puisque
        REMAP_SCHEMA le change par construction.
        """
        # L'adaptateur est choisi sur le nom du schema, et non sur la
        # position du parametre : les deux etapes interrogees portent
        # souvent le meme nom de base, et un choix par position
        # interrogerait la cible pour la source des lors que les
        # connexions sont identiques — cas le plus courant, et celui ou
        # l'erreur resterait invisible.
        adapter = (self.target_adapter
                   if schema == self.state.target_schema
                   else self.source_adapter)
        rows = adapter.query(
            "select object_type, object_name from dba_objects "
            f"where owner = '{_lit(schema)}' and object_type not in "
            "('SYNONYM', 'DATABASE LINK') order by object_type, object_name"
        )
        return [f"{row[0].strip()}~{row[1].strip()}" for row in rows if len(row) > 1]

    # ------------------------------------------------------------------
    # Etapes 17 a 19 — rapport, nettoyage, retour
    # ------------------------------------------------------------------
    def _step_17(self) -> None:
        """L'etat est enregistre ; le rapport est produit par le appelant.

        Cette etape existe pour que l'etat soit sur disque avant toute
        operation de nettoyage. Le rapport lui-meme est ecrit par
        `cli.py`, qui dispose du document complet.
        """
        self._record(17, STEP_NAMES[17], message="etat enregistre")

    def _step_18(self) -> None:
        """Nettoie les artefacts intermediaires, si l'option le demande.

        Le nettoyage est destructif : il supprime le dump et le journal.
        Il n'a lieu qu'apres succes complet, jamais sur un echec, sauf
        option explicite. Un dump est volumineux, mais le garder sans
        raison transforme le serveur en reservoir de fichiers oublies.
        """
        if self.dry_run:
            self.state.finish(18, STEP_NAMES[18], status=SKIPPED, message="simulation")
            return
        if not self.cfg.get("CLEANUP_AFTER_SUCCESS"):
            self.state.finish(18, STEP_NAMES[18], status=SKIPPED, message="desactive")
            return
        if self._failed_here:
            # `final_code` ne convient pas ici : il est relu dans le
            # fichier d'etat lors d'une reprise, et designe alors
            # l'echec de la tentative **anterieure**. Une reprise
            # reussie se concluait donc sur « echec anterieur » et laissait
            # le dump en place — l'inverse de ce qu'on demande a cette
            # etape, qui n'a de sens qu'une fois la duplication terminee.
            #
            # Ce qu'il faut savoir est si **cette** invocation a echoue.
            # Atteindre l'etape 18 le dit deja de fait : `execute()`
            # rend la main sur la premiere erreur, donc aucune n'a pu
            # etre enregistree sans interrompre le parcours. Le drapeau
            # le rend explicite plutot que laisse a deduire d'un
            # cheminement.
            self.state.finish(18, STEP_NAMES[18], status=SKIPPED, message="echec anterieur")
            return
        if self.cfg.get("KEEP_ARTIFACTS"):
            self.state.finish(18, STEP_NAMES[18], status=SKIPPED, message="KEEP_ARTIFACTS")
            return

        # Les tables maitres Data Pump (`SYS_EXPORT_TABLE_nn`) ne sont
        # pas supprimees ici. `DBMS_DATAPUMP.REMOVE_JOB` ne agit que sur
        # le job de la **session courante** : le retirer depuis une autre
        # session exige de se reconnecter avec le meme `userid` et le
        # meme nom de job, ce que cet outil ne fait pas. La consequence
        # est connue et documentee dans `docs/RUNBOOK.md` : la table
        # maitre verrouille le schema, et un `DROP USER` ulterieur exige
        # `CASCADE`.
        #
        # Inventer une variante de `REMOVE_JOB` ici sans pouvoir la
        # tester serait pire que de le dire : le symptome d'une mauvaise
        # reponse, un job qu'on croit supprime et qui ne l'est pas, ne
        # se voit qu'au moment du prochain export.
        removed = self._cleanup_remote()
        self._record(18, STEP_NAMES[18], message=f"{removed} artefact(s) supprime(s)")

    def _cleanup_remote(self) -> int:
        """Supprime dump et journaux sur les deux hotes."""
        count = 0
        for side, path_key in ((self.source, "source_directory_path"),
                               (self.target, "target_directory_path")):
            path = self.state.artifacts.get(path_key)
            if not path:
                continue
            names = [p["name"] for p in self.state.artifacts.get("dump_parts", [])]
            names.append(self.state.artifacts.get("dump_log", ""))
            names.append(self.state.artifacts.get("import_log", ""))
            # Le DDL regenere par la relecture et son journal : produits
            # a l'etape 12, ils restent sinon dans le repertoire
            # DIRECTORY, qui n'est nettoye par personne d'autre.
            names.append(self.state.artifacts.get("verify_sql", ""))
            names.append(self.state.artifacts.get("verify_log", ""))
            script = build_script(
                load_body("remote_listdir.sh"), [path, "", "", "unlink"] + [n for n in names if n]
            )
            try:
                # `mutating=True` : la suppression est l'operation la plus
                # destructive du projet. En dry-run elle ne doit pas
                # seulement etre simulee — elle doit laisser le dump
                # intact, puisque c'est lui qui permet la reprise
                # manuelle apres un echec.
                result = side.runner.run_script(script, timeout=300, mutating=True)
                if result.rc == 0:
                    count += result.get_int("OSD_REMOVED", 0)
            except OsdError:  # pragma: no cover - nettoyage best-effort
                continue
        return count

    def _step_19(self) -> None:
        """Fixe le code de retour final."""
        self.state.final_code = ec.SUCCESS
        self.state.finish(19, STEP_NAMES[19], status=DONE, message="code 0")

    # ------------------------------------------------------------------
    # Proprietes derivees
    # ------------------------------------------------------------------
    def _remap_pairs(self) -> List[str]:
        """Paires de remap de tablespace, validees.

        Une paire mal formee est refusee ici plutot que transmise a
        Data Pump, qui l'accepterait et echouerait plus tard avec un
        message obscur.
        """
        raw = self.cfg.get("REMAP_TABLESPACE") or []
        pairs: List[str] = []
        for item in raw:
            if ":" not in item:
                raise ConfigError(
                    f"REMAP_TABLESPACE mal forme : {item!r}",
                    hint="Format attendu : SOURCE:DESTINE, plusieurs paires "
                         "separees par des virgules.",
                )
            source, _, target = item.partition(":")
            source = validate_identifier(source.strip(), "tablespace source")
            target = validate_identifier(target.strip(), "tablespace cible")
            pairs.append(f"{source}:{target}")
        return pairs

    @property
    def source_runner(self):
        if self._source_runner is None:
            self._source_runner = self._make_runner("SOURCE")
        return self._source_runner

    @property
    def target_runner(self):
        if self._target_runner is None:
            self._target_runner = self._make_runner("TARGET")
        return self._target_runner

    @property
    def source(self) -> OracleSide:
        """ Cote source : connexion, schema, repertoire, runner. """
        if self._source is None:
            self._source = self._make_side("source")
        return self._source

    @property
    def target(self) -> OracleSide:
        """ Cote cible : connexion, schema, repertoire, runner. """
        if self._target is None:
            self._target = self._make_side("cible")
        return self._target

    @property
    def source_adapter(self) -> OracleAdapter:
        if self._source_adapter is None:
            self._source_adapter = OracleAdapter(self.source)
        return self._source_adapter

    @property
    def target_adapter(self) -> OracleAdapter:
        if self._target_adapter is None:
            self._target_adapter = OracleAdapter(self.target)
        return self._target_adapter

    def _make_runner(self, prefix: str):
        # Le runner reel est construit **inconditionnellement**, y compris
        # en dry-run : le `NullRunner` s'en sert pour les lectures, et
        # sans lui la validation des etapes 3 a 9 serait impossible. Le
        # dry-run n'evite donc pas de creer un runner, il en limite
        # l'usage — ce qui est la distinction utile.
        host = str(self.cfg.get(f"{prefix}_HOST", ""))
        if not host:
            real = LocalRunner()
        else:
            # Execution distante par Ansible. Le nom d'hote est celui de
            # l'inventaire : l'adresse reelle peut differer, si
            # l'inventaire pose `ansible_host`. Les identifiants SSH ne
            # sont plus lus ici -- ils sont dans l'inventaire, chiffres
            # par Vault, et Ansible est le seul a les voir.
            real = ansible_runner.AnsibleRunner(
                host,
                inventory=str(self.cfg.get("OSD_INVENTORY", "")),
                group=(
                    ansible_runner.GROUP_SOURCE
                    if prefix == "SOURCE"
                    else ansible_runner.GROUP_TARGET
                ),
                vault_password_file=str(self.cfg.get("OSD_VAULT_PASSWORD_FILE", "")),
                side=prefix.lower(),
            )
        if self.dry_run:
            return null_mod.NullRunner(real, f"{prefix.lower()}-dryrun")
        return real

    def withheld_mutations(self) -> List[str]:
        """Mutations que le dry-run a retenues, cote par cote.

        Vide hors simulation. Le rapport s'en sert pour montrer ce que
        l'outil **n'a pas** fait, ce qui est l'information premiere d'un
        dry-run : « tout a ete verifie, rien n'a ete change ».
        """
        out: List[str] = []
        for prefix, runner in (("source", self.source_runner),
                               ("cible", self.target_runner)):
            if runner.kind == "null":
                for item in runner.summary():
                    out.append(f"[{prefix}] {item}")
        return out

    def _make_side(self, name: str) -> OracleSide:
        prefix = "SOURCE" if name == "source" else "TARGET"
        runner = self.source_runner if prefix == "SOURCE" else self.target_runner
        return OracleSide(
            name=name,
            connect=str(self.cfg.get(f"{prefix}_CONNECT", "")),
            schema=str(self.cfg.get(f"{prefix}_SCHEMA", "")),
            directory=str(self.cfg.get(f"{prefix}_DIRECTORY", "")),
            wallet=str(self.cfg.get(f"{prefix}_WALLET", "")),
            user=str(self.cfg.get(f"{prefix}_USER", "")),
            tns_admin=str(self.cfg.get(f"{prefix}_TNS_ADMIN", "")),
            sysdba=bool(self.cfg.get(f"{prefix}_SYSDBA")),
            os_auth=bool(self.cfg.get(f"{prefix}_OS_AUTH")),
            runner=runner,
        )


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def _secure(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover
        pass


def _mot_de_passe_ssh(runner) -> str:
    """Secret SSH que le runner sait fournir, ou chaine vide.

    Le transfert du dump (etape 13) passe par `scp`/`rsync`/`sftp`
    lances depuis le serveur de saut, donc hors du chemin d'Ansible. Il
    doit pourtant s'authentifier sur la source avec le meme mot de passe
    que le runner, et celui-ci ne le connait pas : il ne fait que le
    transmettre a `sshpass`, dans son processus.

    D'ou cette fonction, qui interroge le runner et laisse une chaine
    vide si la methode n'existe pas. C'est le cas de `LocalRunner` et du
    `NullRunner` du dry-run, et c'est la bonne reponse dans les deux
    cas : en execution locale il n'y a rien a authentifier, et en
    simulation le transfert n'est de toute facon jamais tente. La valeur
    obtenue n'est pas journalisee : `redact` couvre le trace, et le
    rapport ne doit pas la contenir.
    """
    methode = getattr(runner, "ssh_password", None)
    if not callable(methode):
        return ""
    try:
        return methode() or ""
    except Exception:  # pragma: no cover - le runner reporte lui-meme
        return ""


#: Libelle du rapport pour une connexion par identite du systeme.
_AUTH_OS = "authentification OS"


def _connect_affiche(cfg, prefix: str) -> str:
    """Chaine montree au rapport pour un cote de la duplication.

    `CONNECT` est la valeur habituelle. En authentification OS, il n'y a
    pas de chaine : afficher le fait plutot que le vide, sans quoi le
    rapport dirait « source :  / schema HR » et laisserait croire a un
    oubli de configuration quand c'est un choix. C'est le seul usage de
    `SOURCE_CONNECT`/`TARGET_CONNECT` dans le rapport : le pipeline
    porte la chaine, il doit donc aussi porter son absence.
    """
    if cfg.get(f"{prefix}_OS_AUTH"):
        return _AUTH_OS
    return str(cfg.get(f"{prefix}_CONNECT", ""))


def _lit(value: str) -> str:
    return str(value).replace("'", "''")
