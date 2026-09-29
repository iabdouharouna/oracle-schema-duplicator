"""Tests du pipeline : les 19 etapes, le dry-run, la reprise, l'arret.

Le pipeline est le seul endroit ou se rencontrent la configuration, les
adaptateurs, l'etat et le rapport. Ses defauts ne se voient donc ni dans
les tests de `preflight`, qui verifient un controle isole, ni dans ceux
de `datapump`, qui verifient une commande. Ce qui se voit ici, et nulle
part ailleurs, ce sont les proprietes **d'orchestration** :

* le code de sortie est bien celui de l'etape qui a echoue, et pas 0 par
  oubli ou par defaut ;
* l'etat enregistre reellement ce qui s'est passe, sinon une reprise
  rejoue des etapes deja faites, ou en saute d'autres ;
* le dry-run ne modifie **rien**, et le dit ;
* une interruption laisse un etat reprenable.

Une note de methode, parce qu'elle dicte la forme de presque tous les
tests : les quatorze scenarios imposes par `AGENTS.md` sont ici verifies
par **effet**, jamais par inspection de chaine. Un test qui cherche
« simulation » dans un message passerait aussi bien si le message
mentait, et l'utilisateur verrait « rien n'a ete modifie » apres avoir
ecrase une table. Ce qui est observe, c'est le runner : a-t-il recu une
mutation, oui ou non.

`Scenario` fournit par defaut une situation reussie, et chaque test ne
declare que ce qu'il casse. Un test qui echoue pour une raison etrangere
a son objet est un test qui fait perdre du temps, alors qu'il pourrait
faire perdre une donnee.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import support  # noqa: F401

from osd import exit_codes as ec
from osd.adapters.null import NullRunner
from osd.adapters.transfer import TransferBackend
from osd.errors import (
    ConfigError,
    ConnectionError_,
    ExportError,
    OsdError,
    PrereqError,
    TransferError,
)
from osd.stages.pipeline import STEP_NAMES, Pipeline
from osd.runner import Result
from osd.state import DONE, FAILED, SKIPPED, State, StateStore

#: Chaque corps de `shell/` porte un nom propre. Ce tableau permet au
#: faux runner de reconnaitre le script qu'on lui passe a partir de son
#: **contenu**, sans dependre de la position des arguments -- position
#: qui change des qu'un parametre est ajoute, et qui ferait echouer les
#: tests sans dire pourquoi.
MARQUEURS = {
    "espace": "OSD_AVAIL_BYTES",
    "listdir": "OSD_LISTED",
    "pathinfo": "OSD_INTACT",
    "which": "OSD_FOUND",
    "datapump": "OSD_ERROR_CODES",
    "sqlplus": "OSD_ORACLE_CODES",
}

#: Un gibioctet, pour que les estimations d'espace soient lisibles.
GIB = 1024 ** 3

#: Code de sortie Data Pump d'un echec **reel**.
#:
#: Les codes 0, 1, 2, 4 et 8 sont des succes -- 2, 4 et 8 avec reserve.
#: Choisir 1 pour representer un echec est donc une erreur silencieuse :
#: le test passerait sans que rien n'ait echoue. Data Pump reserve 16 et
#: au-dela aux echecs.
ECHEC_DATAPUMP = 16


class FauxRunner:
    """Runner qui repond a chaque corps de `shell/` et **note** les appels.

    Le classement se fait sur le `mutating` declare par l'appelant, et
    non sur le contenu du script : c'est exactement ce que le
    `NullRunner` remplace, donc ce qu'il faut pouvoir observer. Un test
    qui deduirait la mutation du nom du script mesurerait le script, pas
    la retenue.
    """

    def __init__(
        self,
        *,
        label: str = "faux",
        binaires: Sequence[str] = ("expdp", "impdp", "sqlplus"),
        octets_libres: int = 500 * GIB,
        parties: Sequence[str] = ("osd_R1.dmp",),
        taille: int = 12 * 1024 * 1024,
        rc_datapump: Optional[Dict[str, int]] = None,
        codes_oracle: Optional[Dict[str, str]] = None,
        listdir_rc: int = 0,
    ) -> None:
        self.kind = "remote"
        self.host = f"{label}.exemple"
        self.user = "oracle"
        self.probe_dir = f"/donnees/{label}"
        self.ssh_opts: List[str] = ["BatchMode=yes", "ConnectTimeout=10"]
        self.presents = set(binaires)
        self.octets_libres = octets_libres
        self.parties = list(parties)
        self.taille = taille
        #: Nom d'outil -> code de sortie Data Pump a simuler.
        #:
        #: Un code unique par facade ne suffirait pas : l'etape 12 fait
        #: tourner `impdp` sur la **source**, et un scenario qui simule
        #: une relecture de dump tronque simulerait du meme coup un
        #: export en echec. Les deux sont des codes opposes (4 contre 6),
        #: confondus on ne pourrait plus tester aucun des deux.
        self.rc_datapump = dict(rc_datapump or {})
        #: Nom d'outil -> codes `ORA-` presentes dans sa sortie.
        self.codes_oracle = dict(codes_oracle or {})
        self.listdir_rc = listdir_rc
        self.appels: List[Tuple[bool, str]] = []

    @property
    def label(self) -> str:
        return f"faux:{self.host}"

    def has_binary(self, name: str) -> bool:
        return name in self.presents

    def allows_mutation(self) -> bool:
        return True

    # -- Observation -----------------------------------------------------

    def mutations(self) -> List[str]:
        return [script for flag, script in self.appels if flag]

    def lectures(self) -> List[str]:
        return [script for flag, script in self.appels if not flag]

    def scripts_de(self, famille: str) -> List[str]:
        marqueur = MARQUEURS[famille]
        return [s for _, s in self.appels if marqueur in s]

    def args(self, script: str) -> List[str]:
        """Arguments passes au script, dans l'ordre.

        Discrimination par **argument** et non par le corps : le corps de
        `remote_listdir.sh` contient les deux branches (`list` et
        `unlink`), donc chercher le mot `unlink` dans le script repond
        toujours « oui » et fait passer une enumeration pour une
        suppression. C'est exactement le genre de raccourci qui donne un
        faux vert.
        """
        brut: Dict[int, str] = {}
        for ligne in script.splitlines():
            if not ligne.startswith("osd_arg"):
                continue
            numero, _, valeur = ligne.partition("=")
            try:
                index = int(numero[len("osd_arg"):])
            except ValueError:
                continue
            brut[index] = valeur.strip().strip("'")
        return [brut[i] for i in sorted(brut)]

    def outil(self, famille: str = "datapump") -> str:
        """Nom de l'outil demande par le dernier script de `famille`."""
        scripts = self.scripts_de(famille)
        if not scripts:
            return ""
        args = self.args(scripts[-1])
        return args[0] if args else ""

    # -- Execution -------------------------------------------------------

    def run_script(
        self, script: str, *, timeout: Optional[int] = None, mutating: bool = False
    ) -> Result:
        self.appels.append((mutating, script))
        for famille, marqueur in MARQUEURS.items():
            if marqueur in script:
                return getattr(self, f"_reponse_{famille}")(script)
        return Result(rc=0, kv={"OSD_RC": "0"}, command="faux")

    def _reponse_espace(self, script: str) -> Result:
        return Result(
            rc=0,
            kv={"OSD_RC": "0", "OSD_PATH": self.probe_dir,
                "OSD_AVAIL_BYTES": str(self.octets_libres)},
        )

    def _reponse_listdir(self, script: str) -> Result:
        if self.listdir_rc != 0:
            return Result(rc=self.listdir_rc, kv={"OSD_RC": str(self.listdir_rc)})
        if "unlink" in self.args(script):
            return Result(rc=0, kv={"OSD_RC": "0", "OSD_REMOVED": "2"})
        return Result(rc=0, kv={"OSD_RC": "0"}, rows=list(self.parties))

    def _reponse_pathinfo(self, script: str) -> Result:
        return Result(rc=0, kv={"OSD_RC": "0", "OSD_SIZE": str(self.taille)})

    def _reponse_which(self, script: str) -> Result:
        return Result(rc=0, kv={"OSD_RC": "0", "OSD_FOUND": "1"})

    def _reponse_datapump(self, script: str) -> Result:
        args = self.args(script)
        outil = args[0] if args else ""
        return Result(
            rc=0,
            kv={"OSD_RC": str(self.rc_datapump.get(outil, 0)),
                "OSD_ERROR_CODES": self.codes_oracle.get(outil, "")},
        )

    def _reponse_sqlplus(self, script: str) -> Result:
        """Reponse neutre a un script SQL*Plus.

        Ce chemin ne devrait **jamais** etre pris : dans ce harnais,
        l'adaptateur Oracle est faux, donc aucune requete SQL n'atteint
        le runner. L'etat d'un job Data Pump se declare sur
        l'adaptateur, via la reponse `dba_datapump_jobs`, la ou ou le SQL
        est reellement emis.

        Cette reponse ne porte donc aucune donnee, et c'est volontaire.
        Un `job_state` pose ici ne changerait rien, parce que rien ne le
        lirait : un attribut inerte dans un faux est un piege, il donne
        l'illusion d'un levier qui n'a aucun effet. C'est exactement
        l'erreur commise ici avant que l'etat du job soit declare du bon
        cote -- les tests passaient, et ne testaient rien.
        """
        return Result(rc=0, kv={"OSD_RC": "0"})


#: Inventaire d'objets **identique** des deux cotes.
#:
#: Partage entre source et cible, et non deux inventaires coherents
#: ecrits a la main : la comparaison de l'etape 16 porte sur des
#: ensembles d'objets, et un inventaire different ne prouverait que la
#: capacite a detecter une difference -- ce qu'un autre test fait deja.
#: Ici, l'inventaire est une donnee d'arriere-plan, pas le sujet.
_INVENTAIRE = {
    "dba_objects": [
        ["TABLE", "EMPLOYEES"],
        ["TABLE", "DEPARTMENTS"],
        ["INDEX", "EMP_EMP_ID_PK"],
    ],
}


class SchemaQuiDisparait(support.FakeAdapter):
    """Schema cible qui existe au controle 7 et a disparu a la validation.

    Le cas est `DROP USER` pendant l'import : le dump etait complet,
    `impdp` a rendu 0, et la cible est vide. Aucun des deux faits ne se
    voit sur le code de sortie, ce qui est precisement ce que les etapes
    15 et 16 existent pour attraper.

    Un double a etat est necessaire parce que `schema_exists` est une
    **methode** : poser `adapter.schema_exists = False` la remplacerait
    par un booleen, et l'appel transformerait une attribute en
    appelable, produisant un `TypeError` qui ne ressemblerait a rien.
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        #: Nombre de consultations. Annonce par un test : voir
        #: `test_un_schema_cible_disparu_est_un_code_validation`.
        self.vues = 0

    def schema_exists(self, schema: str) -> bool:
        self.vues += 1
        return self.vues == 1


def _reponses(
    privilege: str, job_state: str, job_errors: str, operation: str
) -> Dict[str, Any]:
    """Reponses SQL declarees par defaut, plus l'etat du job.

    Le job est interroge par **deux** requetes distinctes : l'etat
    d'abord, `error_count` ensuite, separement. La colonne peut en
    effet manquer sur certaines vues `DBA_DATAPUMP_JOBS`, et la
    referencee dans la requete principale ferait alors echouer celle-ci
    en entier.

    Le registre de reponses etant indexe par sous-chaine de la requete,
    une seule cle ne peut pas distinguer les deux : il faut une cle par
    requete. Elles sont ici mutuellement exclusives — ni `operation,
    state` ni `error_count` n'apparait dans l'autre requete — donc
    l'ordre de recherche n'a pas d'importance.

    `dba_datapump_jobs` est presente **toujours**, y compris en
    `COMPLETED`, et non seulement quand un test veut un job en erreur.
    Son absence se lirait « etat inconnu », que `_assert_datapump`
    distingue d'un succes -- donc un test qui declare une reponse pour
    un job en echec testerait autre chose qu'un job reellement en
    echec.
    """
    return {
        "session_privs": [[privilege]],
        "operation, state": [["JOB", operation, job_state]],
        "error_count": [[job_errors]],
    }


def _adaptateur_sans_job() -> FakeAdapter:
    """Adaptateur dont la vue `DBA_DATAPUMP_JOBS` ne rend aucune ligne.

    Un job qu'on ne voit pas n'est pas un job en erreur : la session
    peut ne pas avoir les droits, ou la vue peut etre absente. Le
    pipeline doit alors se fier aux deux autres signaux -- code de
    sortie et codes `ORA-`/`UDI-` -- sans en faire un echec.
    """
    return support.FakeAdapter(
        object_count=30, schema_exists=True,
        schema_segment_bytes=8 * GIB,
        responses={"session_privs": [["EXP_FULL_DATABASE"]]},
    )


def _adaptateur_source(
    *, job_state: str = "COMPLETED", job_errors: str = "0", **kw: Any
):
    """Adaptateur source qui reussit tout ce qu'on lui demande par defaut."""
    donnees = dict(
        object_count=30, schema_exists=True,
        schema_segment_bytes=8 * GIB,
        directory_path="/donnees/source",
        tablespaces_capacity={"USERS": {"free_bytes": 100 * GIB}},
        responses=_reponses("EXP_FULL_DATABASE", job_state, job_errors, "EXPORT"),
    )
    donnees["responses"].update(kw.pop("responses", None) or {})
    donnees.update(kw)
    return support.FakeAdapter(**donnees)


def _adaptateur_cible(
    *, job_state: str = "COMPLETED", job_errors: str = "0", **kw: Any
):
    """Adaptateur cible qui reussit tout ce qu'on lui demande par defaut."""
    donnees = dict(
        object_count=0, schema_exists=True,
        directory_path="/donnees/cible",
        # Deux tablespaces, et non un : `REMAP_TABLESPACE` verifie la
        # cible **apres remap**, donc un test de remap qui n'offreait
        # qu'un seul nom ne pourrait pas distinguer « le remap a ete
        # applique » de « le remap a ete casse et l'echeance est la
        # seule explication du refus ».
        tablespaces_capacity={"USERS": {"free_bytes": 100 * GIB},
                             "UTILITY": {"free_bytes": 100 * GIB}},
        responses=_reponses("IMP_FULL_DATABASE", job_state, job_errors, "IMPORT"),
    )
    donnees["responses"].update(kw.pop("responses", None) or {})
    donnees.update(kw)
    return support.FakeAdapter(**donnees)


class Scenario:
    """Un pipeline complet, arme par defaut pour reussir.

    `TRANSFER_MODE=local` est pose par defaut, ce qui **n'est pas** un
    detail : la branche `relais` lancerait une sonde de transfert reelle,
    donc un `subprocess` vers un hote inexistant. Un test qui poserait
    par defaut le mode `AUTO` testerait le reseau, pas le pipeline. La
    branche `relais` est couverte par ses propres tests, ou le backend
    est remplace.
    """

    #: Repertoire partage : la topologie nominale des tests.
    TRANSFERT_DEFAUT = "local"

    def __init__(
        self,
        *,
        racine: Path,
        dry_run: bool = False,
        allow_destructive: bool = False,
        resume: bool = False,
        force: bool = False,
        only: Optional[Sequence[int]] = None,
        source: Optional[Any] = None,
        cible: Optional[Any] = None,
        source_runner: Optional[FauxRunner] = None,
        config: Optional[Dict[str, str]] = None,
    ) -> None:
        self.racine = racine
        self.dry_run = dry_run
        self.run_id = "R1"
        self.source_runner = source_runner or FauxRunner(label="source")
        self.target_runner = FauxRunner(label="cible")
        self.source_adapter = source if source is not None else _adaptateur_source()
        self.target_adapter = cible if cible is not None else _adaptateur_cible()

        surcharges: Dict[str, str] = {
            "SOURCE_HOST": "source.exemple",
            "TARGET_HOST": "cible.exemple",
            "LOG_DIR": str(racine / "logs"),
            "WORK_DIR": str(racine / "work"),
            "REPORT_DIR": str(racine / "reports"),
            "TRANSFER_MODE": self.TRANSFERT_DEFAUT,
        }
        for cle, valeur in dict(config or {}).items():
            surcharges[cle.upper()] = valeur
        self.cfg = support.load_config(**surcharges)

        # La substitution par `NullRunner` est faite par
        # `Pipeline._make_runner`, chemin unique en production. Injecter
        # un runner court-circuite ce chemin : c'est donc a ce harnais de
        # le reproduire, sinon les tests de simulation valideraient un
        # runner **reel** et l'assertion « aucune mutation executee »
        # passerait a vide.
        self.reel_source = self.source_runner
        self.reel_cible = self.target_runner
        if dry_run:
            self.source_runner = NullRunner(self.source_runner, "source-dryrun")
            self.target_runner = NullRunner(self.target_runner, "cible-dryrun")

        self.state = State(run_id=self.run_id)
        self.pipeline = Pipeline(
            cfg=self.cfg, state=self.state, run_id=self.run_id,
            dry_run=dry_run, allow_destructive=allow_destructive,
            resume=resume, force=force, only=only,
            _source_runner=self.source_runner,
            _target_runner=self.target_runner,
            _source_adapter=self.source_adapter,
            _target_adapter=self.target_adapter,
        )

    # -- Conduite --------------------------------------------------------

    def aller(self) -> int:
        return self.pipeline.execute()

    def etape(self, index: int):
        return self.state.steps.get(index)

    def statut(self, index: int) -> str:
        st = self.etape(index)
        return st.status if st else ""

    def mutations_reelles(self) -> List[str]:
        """Mutations reellement confiees a un runner reel.

        En simulation, cette liste doit etre **vide** : c'est la seule
        mesure qui ne depend d'aucune promesse du rapport.
        """
        out: List[str] = []
        for runner in (self.reel_source, self.reel_cible):
            out.extend(runner.mutations())
        return out

    def lectures_reelles(self) -> List[str]:
        """Lectures effectivement deleguees aux temoins."""
        out: List[str] = []
        for runner in (self.reel_source, self.reel_cible):
            out.extend(runner.lectures())
        return out

    # -- Aides de scenario ----------------------------------------------

    def marquer_validees(self, indices) -> None:
        """Marque des etapes comme validees, puis relit l'etat du disque.

        Le round-trip par `State.from_dict` est le meme que fait un
        `resume`. Sans lui, ces tests repartaient d'un etat en memoire :
        aucune etape n'y portait la trace d'avoir ete relue, et le
        rapport pouvait donc presenter des etapes d'une tentative
        anterieure comme celles de ce run.

        C'est ce que verifie le test du rapport — steps 15 a 19 affichees
        « OK », « 19. retourner-code : code 0 », sur un processus qui
        rendait 6. Le trou etait dans le harnais avant d'etre dans le
        code.
        """
        for index in indices:
            self.state.start(index, STEP_NAMES[index])
            self.state.finish(index, STEP_NAMES[index], status=DONE, message="fait")
        self.state = State.from_dict(self.state.to_dict())
        self.pipeline.state = self.state

    def deja_avance(self, jusqu_a: int) -> None:
        self.marquer_validees(range(1, jusqu_a + 1))

    def rapport(self) -> Dict[str, Any]:
        """Construit le document de rapport comme le fait `cli.py`.

        Le rapport est construit par une fonction **sans etat**, qui
        recoit tout : le reconstruire ici avec des argumentsdifferent de
        ceux de la production testerait un autre chemin de code que celui
        qui produit reellement le rapport.
        """
        from osd.report import build_document

        return build_document(
            cfg=self.cfg,
            state=self.state,
            checks=self.pipeline.checks,
            started_at=0.0,
            finished_at=1.0,
            error=self.state.error,
            simulated=self.pipeline.withheld_mutations(),
        )

    def artefacts_de_datapump(self) -> None:
        """Pose les artefacts que les etapes 10 a 12 auraient produits.

        Indispensable pour tester une etape isolee : sans eux, l'etape 13
        echouerait sur une absence de parties, ce qui testerait autre
        chose.
        """
        self.state.artifacts.update({
            "job_name": "OSD_R1",
            "dump_base": "osd_R1",
            "dump_log": "osd_R1_export.log",
            "import_log": "osd_R1_import.log",
            "dumpfile_spec": "osd_R1.dmp",
            "dump_parts": [{"name": "osd_R1.dmp", "bytes": 12 * 1024 * 1024}],
            "source_directory_path": "/donnees/source",
            "target_directory_path": "/donnees/cible",
        })


class CasDeTest(unittest.TestCase):
    """Base : une racine temporaire et un `Scenario` neuf par test.

    `nouveau()` est appele explicitement plutot que fait en `setUp` :
    beaucoup de tests doivent modifier le scenario avant la premiere
    etape, et un scenario construit trop tot obligerait a le refaire
    apres coup.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.racine = Path(self._tmp.name)

    def nouveau(self, **kw: Any) -> Scenario:
        return Scenario(racine=self.racine, **kw)


class TestSpecificationDImport(CasDeTest):
    """Ce que l'import recoit comme `DUMPFILE`.

    La decision est prise ici, pas dans le parfile : le parfile ne
    fait que transmettre une chaine, et c'est le pipeline qui connait
    les parties reellement produites.
    """

    def _spec_avec(self, parties) -> str:
        s = self.nouveau()
        s.state.artifacts["dump_parts"] = parties
        return s.pipeline._spec_dimport()

    def test_la_specification_utilise_les_noms_reels(self):
        """Ni jeton, ni nom unique devine : la liste des parties.

        `%d` est une variable de substitution propre a l'**export**.
        `impdp` la refuse, par `ORA-39124: ... contient une variable de
        substitution non valide`. La reutiliser rendait l'etape 12 --
        puis l'etape 14, qui partageait la meme valeur -- impossible a
        reussir sur un dump parfaitement complet, et le journal
        Data Pump ne montrait qu'une substitution invalide, sans aucun
        rapport avec le dump lui-meme.

        Le comportement est verifie sur une vraie base 19c : le nom
        concret relit 801 lignes de DDL, la forme a jeton echoue.
        L'ordre est trie, donc reproductible d'un run a l'autre.
        """
        self.assertEqual(
            self._spec_avec([
                {"name": "osd_R1.dmp", "bytes": 1},
                {"name": "osd_R1-29.dmp", "bytes": 1},
            ]),
            "osd_R1-29.dmp,osd_R1.dmp",
        )

    def test_une_partie_unique_ne_donne_pas_de_virgule(self):
        self.assertEqual(
            self._spec_avec([{"name": "seule.dmp", "bytes": 1}]),
            "seule.dmp",
        )

    def test_sans_partie_connue_l_import_est_refuse(self):
        """Un `resume` sautant l'etape 11 doit le dire, pas deviner.

        Les parties sont ce que l'etape 11 a observe. Sans elles,
        l'import viserait un nom construit, que `impdp` refuse -- ou pire,
        un fichier qui ne serait pas celui du run. L'echec est donc
        explicite, avec le remede.
        """
        s = self.nouveau()
        s.state.artifacts["dump_parts"] = []
        with self.assertRaises(ExportError) as ctx:
            s.pipeline._spec_dimport()
        self.assertIn("partie", str(ctx.exception))
        # Le remede doit nommer la reprise : c'est l'action que
        # l'exploitant a sous la main, et elle n'est pas devinable.
        self.assertIn("resume", (ctx.exception.hint or "").lower())
        self.assertEqual(ctx.exception.code, ec.EXPORT)

    def test_une_partie_sans_nom_est_ignoree(self):
        """Une entree incomplete ne doit pas produire de virgule ni de vide.

        Une partie sans nom n'est pas une partie : la filtrer evite un
        `DUMPFILE` mal forme, que `impdp` refuserait sans dire pourquoi.
        """
        self.assertEqual(
            self._spec_avec([
                {"name": "a.dmp", "bytes": 1},
                {"bytes": 10},
                {"name": "", "bytes": 10},
            ]),
            "a.dmp",
        )

    def test_en_simulation_l_absence_est_dite_plutot_que_nommee(self):
        """Un dry-run n'a rien produit : il ne doit pas inventer un nom.

        Le rapport est une simulation, mais y afficher la forme a jeton
        montrerait une specification que l'import refuserait, et que
        personne ne doit pouvoir prendre pour ce qui serait reellement
        execute. Le marqueur se reconnait a son premier coup d'oeil.
        """
        s = self.nouveau(dry_run=True)
        s.state.artifacts["dump_parts"] = []
        self.assertIn("<parties>", s.pipeline._spec_dimport())


class TestCheminNominal(CasDeTest):
    def test_les_dix_neuf_etapes_sont_executees(self):
        s = self.nouveau()
        self.assertEqual(s.aller(), ec.SUCCESS)
        for index in range(1, 20):
            self.assertEqual(
                s.statut(index), DONE,
                f"etape {index:02d} ({STEP_NAMES[index]}) : {s.statut(index)}",
            )

    def test_le_code_de_succes_est_zero(self):
        self.assertEqual(self.nouveau().aller(), ec.SUCCESS)

    def test_l_etat_final_porte_zero(self):
        s = self.nouveau()
        s.aller()
        self.assertEqual(s.state.final_code, ec.SUCCESS)

    def test_chaque_etape_est_nommee_dans_l_etat(self):
        """Une etape sans nom lisible est inexploitable au diagnostic.

        Le `state` ne porte que des indices ; le nom est ce qui permet a
        l'exploitant de reconnaitre l'etape dans le rapport et dans le
        `crontab` qui l'a declenchee.
        """
        s = self.nouveau()
        s.aller()
        noms = {st.name for st in s.state.steps.values()}
        for index in range(1, 20):
            self.assertIn(STEP_NAMES[index], noms)

    def test_l_export_precede_la_verification_du_dump(self):
        """L'ordre porte un sens operationnel.

        Verifier un dump avant de l'avoir produit indiquerait que l'etat
        a ete conserve d'un run precedent -- symptome d'une reprise mal
        bornee, et d'un `resume` qui ne rejouerait pas l'etape 11.
        """
        s = self.nouveau()
        s.aller()
        self.assertIn("dump relu", s.etape(12).message)
        self.assertTrue(s.state.artifacts["dump_verified"])
        self.assertTrue(s.state.artifacts["imported"])

    def test_les_metadonnees_des_deux_cotes_sont_posees(self):
        s = self.nouveau()
        s.aller()
        self.assertIn("source", s.state.metrics)
        self.assertIn("target", s.state.metrics)
        self.assertEqual(s.state.metrics["source_objects"], 30)

    def test_le_rapport_ne_fuit_aucune_chaine_de_connexion(self):
        """Le rapport est le seul support de la decision.

        Il part en piece jointe de ticket, donc vers quelqu'un qui n'a
        pas les memes droits que l'exploitant. Le controle est
        mecanique : on cherche la forme d'un mot de passe, pas une liste
        de chaines autorisees.
        """
        s = self.nouveau()
        s.aller()
        texte = json.dumps(s.rapport(), default=str)
        self.assertNotIn("@//", texte)
        self.assertNotIn("IDENTIFIED BY", texte)

    def test_le_rapport_est_serialisable(self):
        """Un rapport qui ne se serialise pas ne part jamais en ticket."""
        s = self.nouveau()
        s.aller()
        document = s.rapport()
        json.dumps(document)  # doit lever si un type n'est pas JSON
        self.assertIn("steps", document)


# --------------------------------------------------------------------------
# Connexions indisponibles
# --------------------------------------------------------------------------

class TestConnexionsIndisponibles(CasDeTest):
    def test_source_injoignable_rend_le_code_connexion(self):
        """Un client qui refuse la connexion est un code 3, pas un 2.

        Rien ne manque et la configuration est correcte : le code doit
        envoyer vers la connectivite, sinon l'exploitant cherchera une
        concession de privileges qui n'a rien a voir.
        """
        s = self.nouveau(source=_adaptateur_source(
            check_connection=ConnectionError_(
                "connexion source impossible", detail=["ORA-12541: no listener"]
            )
        ))
        self.assertEqual(s.aller(), ec.CONNECTION)
        self.assertEqual(s.statut(4), FAILED)
        self.assertEqual(s.statut(5), "")

    def test_cible_injoignable_rend_le_code_connexion(self):
        s = self.nouveau(cible=_adaptateur_cible(
            check_connection=ConnectionError_("connexion cible impossible")
        ))
        self.assertEqual(s.aller(), ec.CONNECTION)
        self.assertEqual(s.statut(5), FAILED)
        # La source a ete validee : le dire evite de refaire le diagnostic.
        self.assertEqual(s.statut(4), DONE)

    def test_une_instance_fermee_est_refusee_avant_agir(self):
        """Une base fermee n'echouerait qu'a l'import, apres transfert.

        Le controle est fait a l'etape de connexion, donc le code est 3
        et non 2 : le remede est de demarrer l'instance, ce qui est une
        question de **connexion** autant que de prerequis. Ce qui compte
        ici, c'est que le refus intervienne avant l'etape 13.
        """
        s = self.nouveau(cible=_adaptateur_cible(_connect_info={
            "version": "19.0.0.0.0", "instance": "UAT", "dbname": "UAT",
            "status": "DOWN", "open_mode": "", "cdb": "NO",
            "session_user": "SYS", "host": "cible",
        }))
        self.assertEqual(s.aller(), ec.CONNECTION)
        self.assertIn("DOWN", s.state.error["message"])
        self.assertEqual(s.statut(13), "")

    def test_une_version_non_19c_est_un_avertissement_pas_un_refus(self):
        """Bloquer sur ce detail rendrait l'outil inutilisable.

        19c applique par des RUs reste 19c, et un avertissement qui
        bloquerait exclurait la majorite des installations de
        production conformes.
        """
        s = self.nouveau(cible=_adaptateur_cible(_connect_info={
            "version": "12.2.0.1.0", "instance": "UAT", "dbname": "UAT",
            "status": "OPEN", "open_mode": "READ WRITE", "cdb": "NO",
            "session_user": "SYS", "host": "cible",
        }))
        s.aller()
        self.assertTrue(any("12.2" in c.message for c in s.pipeline.checks))

    def test_une_erreur_oracle_brute_est_remontee_telle_quelle(self):
        """Le code Oracle doit etre lisible dans l'etat d'echec.

        Un ticket d'incident commence par la recopie de cette ligne ;
        paraphraser le message ferait perdre le seul identificateur
        qu'un exploitant peut rechercher.
        """
        s = self.nouveau(source=_adaptateur_source(
            check_connection=ConnectionError_(
                "connexion source impossible",
                detail=["ORA-01017: invalid username/password; logon denied"],
            )
        ))
        s.aller()
        detail = " ".join(s.state.error["detail"])
        self.assertIn("ORA-01017", detail)


# --------------------------------------------------------------------------
# Prerequis insatisfaits
# --------------------------------------------------------------------------

class TestPrerequisInsatisfaits(CasDeTest):
    def test_un_client_absent_rend_le_code_prerequis(self):
        s = self.nouveau()
        s.source_runner.presents.discard("expdp")
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertEqual(s.statut(3), FAILED)
        self.assertIn("expdp", s.state.error["message"])

    def test_un_sqlplus_absent_cote_cible_est_signale(self):
        s = self.nouveau()
        s.target_runner.presents.discard("sqlplus")
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertIn("sqlplus", s.state.error["message"])

    def test_un_schema_source_absent_rend_le_code_prerequis(self):
        s = self.nouveau(source=_adaptateur_source(schema_exists=False))
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertEqual(s.statut(6), FAILED)
        self.assertIn("n'existe pas", s.state.error["message"])

    def test_un_schema_source_vide_est_refuse(self):
        """Un schema vide produirait un dump vide dont l'import reussirait.

        C'est le pire genre d'echec : code 0, rapport favorable, et
        aucune donnee de l'autre cote. Le refus doit venir du schema
        vide, pas de l'import.
        """
        s = self.nouveau(source=_adaptateur_source(object_count=0))
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertIn("aucun objet", s.state.error["message"])

    def test_un_directory_absent_est_refuse(self):
        """L'objet DIRECTORY peut exister sans etre accessible.

        C'est le cas d'un volume de donnees non monte au demarrage : le
        dictionnaire est intact, le systeme de fichiers ne l'est pas.
        """
        s = self.nouveau(source=_adaptateur_source(directory_path=""))
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertEqual(s.statut(8), FAILED)

    def test_un_directory_asm_est_refuse(self):
        """Les scripts distants n'ont pas acces a ASM.

        Le dump ecrit dans un chemin ASM ne peut etre ni liste, ni
        mesure, ni transfere : le refuser ici evite un echec a l'etape
        12 sans lien avec sa cause.
        """
        s = self.nouveau(source=_adaptateur_source(is_asm_directory=True))
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertIn("ASM", s.state.error["message"])

    def test_un_tablespace_inexistant_est_refuse(self):
        """Le tablespace verifie est celui de la cible **apres remap**.

        Verifier ceux de la source serait une erreur : ils n'ont pas a
        exister sur la cible. Le remap est donc resolu avant le controle.
        """
        s = self.nouveau(config={"REMAP_TABLESPACE": "USERS:ABSENT"})
        s.aller()
        self.assertEqual(s.statut(8), FAILED)
        self.assertIn("ABSENT", json.dumps(s.state.error, default=str))

    def test_un_remap_valide_ne_verifie_pas_les_tablespaces_source(self):
        """Les tablespaces source n'ont ni a exister ni a etre libres.

        Les verifier enverrait vers un schema cible qui, lui, n'a rien a
        faire de `USERS` sur la source.
        """
        s = self.nouveau(config={"REMAP_TABLESPACE": "USERS:UTILITY"})
        self.assertEqual(s.aller(), ec.SUCCESS)
        messages = " ".join(c.message for c in s.pipeline.checks)
        self.assertIn("UTILITY", messages)
        # Le nom verifie est le **destination**, jamais la source : sinon
        # le controle exigerait que `USERS` existe sur la cible, ce qui
        # n'a aucun sens.
        self.assertNotIn("USERS", messages)

    def test_espace_disque_insuffisant_est_refuse_avant_l_export(self):
        """Le refus doit venir **avant** l'export, pas apres.

        Un export echoue sur disque plein a deja consomme du temps, du
        volume reseau et un journal a diagnostiquer. Le controle d'espace
        existe precisement pour cela.
        """
        s = self.nouveau()
        s.target_runner.octets_libres = 1024
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertEqual(s.statut(9), FAILED)
        self.assertEqual(
            s.source_runner.scripts_de("datapump"), [],
            "un export a ete lance malgre l'espace insuffisant",
        )

    def test_espace_tablespace_insuffisant_est_refuse(self):
        s = self.nouveau(cible=_adaptateur_cible(
            tablespaces_capacity={"USERS": {"free_bytes": 1024}}
        ))
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertIn("insuffisante", s.state.error["message"])

    def test_une_mesure_impossible_ne_provoque_pas_de_refus(self):
        """« je ne sais pas » n'est pas « il n'y a pas de place ».

        Refuser sur une mesure impossible transformerait toute
        restriction d'acces au systeme de fichiers en blocage du run.
        L'incertitude est signalee, le run continue.
        """
        s = self.nouveau()
        s.target_runner.run_script = _sans_reponse(s.target_runner)
        self.assertEqual(s.aller(), ec.SUCCESS)
        skip = [c for c in s.pipeline.checks if c.status == "SKIP"]
        self.assertTrue(skip)

    def test_metadata_only_demande_moins_de_place_que_all(self):
        """En `METADATA_ONLY` le dump ne porte que le DDL.

        Exiger la place des segments ferait echouer une operation
        parfaitement faisable sur une source volumineuse. Le test est
        **comportemental** : meme disque des deux cotes, seul `CONTENT`
        change, et le verdict doit changer. Une comparaison de deux
        estimations ne prouverait que l'arithmetique du facteur.
        """
        cible = 2 * GIB
        meta = self.nouveau(config={"CONTENT": "METADATA_ONLY"})
        meta.target_runner.octets_libres = cible
        self.assertEqual(meta.aller(), ec.SUCCESS)

        tout = self.nouveau(config={"CONTENT": "ALL"})
        tout.target_runner.octets_libres = cible
        self.assertEqual(tout.aller(), ec.PREREQ)


def _sans_reponse(runner: FauxRunner):
    """Rend le `run_script` muet : aucune reponse exploitable.

    Sert a reproduire un acces refuse au systeme de fichiers, ou un
    script dont le bloc de resultat est illisible.
    """
    def _muet(script, *, timeout=None, mutating=False):
        runner.appels.append((mutating, script))
        return Result(rc=1, kv={}, command="muet")

    return _muet


# --------------------------------------------------------------------------
# Privileges
# --------------------------------------------------------------------------

class TestPrivileges(CasDeTest):
    def test_des_privileges_insuffisants_sont_refuses_avant_l_export(self):
        """Le controle existe dans `preflight` : il doit etre cable.

        Un compte sans `EXP_FULL_DATABASE` ni acces au DIRECTORY echoue
        a l'export, apres avoir occupe le repertoire du dump et le
        dictionnaire. Le refuser a l'etape 8 evite ce travail et son
        diagnostic obscur.
        """
        s = self.nouveau(source=_adaptateur_source(
            responses={"session_privs": [["CREATE SESSION"]]}
        ))
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertEqual(s.statut(8), FAILED)
        self.assertIn("EXP_FULL_DATABASE", json.dumps(s.state.error, default=str))
        self.assertEqual(s.source_runner.scripts_de("datapump"), [])

    def test_le_privilege_d_import_est_verifie_separement(self):
        """Un compte d'export n'a pas automatiquement les droits d'import.

        Les confondre produirait un avertissement sur un run valide, et
        laisserait passer l'import sans les droits necessaires.
        """
        s = self.nouveau(cible=_adaptateur_cible(
            responses={"session_privs": [["EXP_FULL_DATABASE"]]}
        ))
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertIn("IMP_FULL_DATABASE", json.dumps(s.state.error, default=str))

    def test_un_acces_au_directory_seul_est_un_avertissement(self):
        """`READ,WRITE ON DIRECTORY` limite l'export sans l'interdire.

        Un refus a cet endroit ecarterait des configurations reellement
        fonctionnelles pour une duplication de schema simple.
        """
        s = self.nouveau(source=_adaptateur_source(
            responses={"session_privs": [["READ,WRITE ON DIRECTORY"]]}
        ))
        s.aller()
        self.assertEqual(s.statut(8), DONE)
        self.assertTrue([c for c in s.pipeline.checks if c.status == "WARN"])

    def test_une_lecture_impossible_des_privileges_est_signalee(self):
        """Une session qui ne peut pas lire ses propres privileges est bloquee.

        `SESSION_PRIVS` est accessible a tout compte connecte : son
        echec de lecture ne signifie donc pas « privileges insuffisants »
        mais « on ne sait pas verifier ». Le controle doit le dire sans
        pretendre que le compte est en faute, et nommer la vue pour que
        l'exploitant puisse reproduire le controle a la main.
        """
        s = self.nouveau(source=_adaptateur_source(
            responses={"session_privs": PrereqError(
                "ORA-00942: table or view does not exist"
            )}
        ))
        self.assertEqual(s.aller(), ec.PREREQ)
        rapport = json.dumps(s.state.error, default=str)
        self.assertIn("SESSION_PRIVS", rapport)
        self.assertIn("ORA-00942", rapport)

    def test_le_controle_passe_apres_les_connexions(self):
        """L'ordre est choisi : un mot de passe expire ne doit pas
        etre rapporte comme un manque de privileges.

        Le code 2 (prerequis) enverrait vers une concession de privileges
        la ou le probleme est une authentification. Le controle des
        privileges est donc pose apres la verification de connexion des
        deux cotes.
        """
        s = self.nouveau(source=_adaptateur_source(
            check_connection=ConnectionError_("connexion source impossible"),
            responses={"session_privs": [["CREATE SESSION"]]},
        ))
        self.assertEqual(s.aller(), ec.CONNECTION)
        self.assertNotIn("EXP_FULL_DATABASE", json.dumps(s.state.error, default=str))


# --------------------------------------------------------------------------
# Garde-fous de securite
# --------------------------------------------------------------------------

class TestGardeFousDeSecurite(CasDeTest):
    def test_un_schema_cible_peuple_est_un_code_securite(self):
        """Le refus est un code 8, non un code 2.

        Un schema inexistant est un prerequis manquant ; un schema peuple
        sans autorisation est un exces de pouvoir. Confondre les deux
        envoie l'exploitant chercher une base qui, elle, existe.
        """
        s = self.nouveau(cible=_adaptateur_cible(object_count=14))
        self.assertEqual(s.aller(), ec.SECURITY)
        self.assertEqual(s.statut(7), FAILED)
        self.assertIn("14", s.state.error["message"])

    def test_le_meme_schema_avec_autorisation_passe(self):
        s = self.nouveau(
            config={"ALLOW_EXISTING_TARGET": "true"},
            source=_adaptateur_source(responses=_INVENTAIRE),
            cible=_adaptateur_cible(
                # Un compte unique repondrait 14 a la question « combien
                # d'objets **invalides** ? » : le validation de l'etape 15
                # verrait un schema entierement invalide. Les deux
                # nombres sont des reponses distinctes, pas un detail.
                object_count={"": 14, "INVALID": 0},
                responses=_INVENTAIRE,
            ),
        )
        self.assertEqual(s.aller(), ec.SUCCESS)
        self.assertEqual(s.statut(7), DONE)

    def test_la_configuration_refuse_d_abord_le_meme_schema(self):
        """Le refus est pose deux fois, et c'est la premiere qui compte.

        `REMAP_SCHEMA` ne sert a rien quand le nom est le meme :
        l'import « reussirait » en ecrasant le schema qu'il vient de
        lire, et le rapport dirait « 30 objets reconcilies ».

        La **configuration** leve deja une `ConfigError`, donc le code
        rendu est 1 et non 8. Le test le verifie explicitement plutot
        que d'attendre 8 : ecrire 8 ferait passer le test si la
        validation disparaitrait, et le remede — corriger la
        configuration — resterait le bon.
        """
        with self.assertRaises(ConfigError):
            self.nouveau(config={"TARGET_SCHEMA": "SRC"})

    def test_le_pipeline_refuse_aussi_le_meme_schema(self):
        """Le garde-fou de l'etape 2 est la seconde ligne.

        Il n'est pas atteignable par la configuration, parce que la
        validation la refuse avant. Il couvre le cas ou la validation est
        contournee : une reprise qui relit un etat produit par une
        version anterieure, ou un appel programmatique. Le test simule
        exactement cela en modifiant l'etat apres construction.
        """
        s = self.nouveau()
        s.state.source_schema = "TGT"
        self.assertEqual(s.aller(), ec.SECURITY)
        self.assertEqual(s.statut(2), FAILED)

    def test_replace_sans_autorisation_est_un_code_securite(self):
        s = self.nouveau(config={"TABLE_EXISTS_ACTION": "REPLACE"})
        self.assertEqual(s.aller(), ec.SECURITY)
        self.assertEqual(s.statut(2), FAILED)

    def test_truncate_sans_autorisation_est_un_code_securite(self):
        s = self.nouveau(config={"TABLE_EXISTS_ACTION": "TRUNCATE"})
        self.assertEqual(s.aller(), ec.SECURITY)

    def test_skip_n_autorise_aucune_destruction(self):
        """Le refus doit porter sur les seules actions destructives.

        Un refus general forcerait l'exploitant a poser
        `--allow-destructive` par habitude, ce qui detruit sa valeur de
        garde-fou.
        """
        s = self.nouveau(config={"TABLE_EXISTS_ACTION": "SKIP"})
        self.assertEqual(s.aller(), ec.SUCCESS)

    def test_avec_autorisation_replace_passe(self):
        s = self.nouveau(
            allow_destructive=True,
            config={"TABLE_EXISTS_ACTION": "REPLACE",
                    "ALLOW_EXISTING_TARGET": "true"},
        )
        self.assertEqual(s.aller(), ec.SUCCESS)

    def test_le_garde_fou_de_l_import_est_rejoue_indepamment(self):
        """L'etape 14 refait le controle, meme sans passer par l'etape 2.

        Redondance deliberee : c'est le dernier point avant l'ecriture,
        et un `resume` qui ne rejouerait que l'import ne repasserait pas
        par la validation. Le test le prouve en **sautant** l'etape 2.
        """
        s = self.nouveau(only=[13, 14], config={"TABLE_EXISTS_ACTION": "REPLACE"})
        s.artefacts_de_datapump()
        self.assertEqual(s.aller(), ec.SECURITY)
        self.assertEqual(s.statut(14), FAILED)
        self.assertEqual(s.target_runner.scripts_de("datapump"), [])

    def test_un_remap_mal_forme_est_un_code_configuration(self):
        s = self.nouveau(config={"REMAP_TABLESPACE": "USERS"})
        self.assertEqual(s.aller(), ec.CONFIG)
        self.assertIn("USERS", s.state.error["message"])

    def test_keep_artifacts_desactive_le_nettoyage(self):
        s = self.nouveau(config={"CLEANUP_AFTER_SUCCESS": "true",
                                 "KEEP_ARTIFACTS": "true"})
        s.aller()
        self.assertEqual(s.statut(18), SKIPPED)
        self.assertIn("KEEP_ARTIFACTS", s.etape(18).message)

    def test_le_nettoyage_est_saute_apres_un_echec(self):
        """Supprimer le dump apres un echec empecherait toute reprise.

        Le dump est la seule chose qui permette de relancer l'import sans
        refaire l'export. Le nettoyer sur un echec obligerait a
        recommencer un run de plusieurs heures depuis le debut.

        L'etape 18 n'est pas « sautee » : elle n'est **pas atteinte**.
        `execute()` rend la main sur la premiere erreur, donc le statut
        n'existe pas -- ce qui est plus fort qu'un `skipped`, et lisible
        sans ambiguite par quiconque cherche la cause dans l'etat.
        """
        s = self.nouveau(config={"CLEANUP_AFTER_SUCCESS": "true"})
        s.source_runner.rc_datapump = {"expdp": ECHEC_DATAPUMP}
        self.assertEqual(s.aller(), ec.EXPORT)
        self.assertEqual(s.statut(18), "")
        retraits = [sc for sc in s.mutations_reelles() if "unlink" in sc]
        self.assertEqual(retraits, [], "le dump a ete supprime malgre l'echec")

    def test_le_nettoyage_a_lieu_apres_un_succes(self):
        """Le nettoyage est une mutation : il doit etre declare comme telle.

        Non declare, il s'executerait **reellement** en simulation et
        detruirait le dump -- la seule chose qui rende le run
        reprenable apres un echec.
        """
        s = self.nouveau(config={"CLEANUP_AFTER_SUCCESS": "true"})
        s.aller()
        self.assertEqual(s.statut(18), DONE)
        # Le nombre d'artefacts n'est pas fige : parties du dump, journaux
        # d'export et d'import. Ce qui compte est qu'il soit **non nul**,
        # et que la suppression soit partie dans les mutations declarees.
        self.assertRegex(s.etape(18).message, r"\d+ artefact\(s\) supprime\(s\)")
        retraits = [sc for sc in s.target_runner.mutations() if "unlink" in sc]
        self.assertTrue(retraits, "le nettoyage n'a pas ete declare mutating")

    def test_le_nettoyage_emporte_le_ddl_de_la_relecture(self):
        """Le SQLFILE de l'etape 12 est un artefact du run, comme le dump.

        Il n'etait pas supprime : produit dans le repertoire DIRECTORY,
        il y restait apres le run, et le DIRECTORY n'appartient a personne
        d'autre. Verifie sur une 19c reelle, ou le DIRECTORY senait
        `osd_verify_*.sql` et `osd_verify_*.log` a chaque passage.

        Les deux noms sont annonces a l'etape 10, ou le nettoyage peut
        les atteindre : nommes a l'etape 12, ils seraient invisibles.
        """
        s = self.nouveau(config={"CLEANUP_AFTER_SUCCESS": "true"})
        s.aller()
        deman = [sc for sc in s.source_runner.mutations() if "unlink" in sc]
        self.assertTrue(deman, "aucun nettoyage sur la source")
        for cle in ("verify_sql", "verify_log", "dump_log", "import_log"):
            with self.subTest(artefact=cle):
                nom = s.state.artifacts[cle]
                self.assertIn(nom, deman[-1],
                              f"{nom} absent du nettoyage")

    def test_le_nettoyage_passe_un_motif_vide(self):
        """`unlink` recoit des noms, pas un motif — et le script l'exige.

        Le motif n'a aucun sens en suppression : l'usage documente est
        `'' '' unlink <noms...>`. Exiger un motif non vide ici
        condamnait le nettoyage entier, et l'etape 18 annoncait alors
        « 0 artefact(s) supprime(s) » sans erreur — le dump et les
        journaux restaient en place, sans trace de ce qui avait ete
        refuse.
        """
        s = self.nouveau(config={"CLEANUP_AFTER_SUCCESS": "true"})
        s.aller()
        for sc in s.source_runner.mutations():
            if "unlink" in sc:
                self.assertIn("'' '' unlink", sc)


# --------------------------------------------------------------------------
# Echec d'export
# --------------------------------------------------------------------------

class TestEchecExport(CasDeTest):
    def test_un_export_en_echec_rend_le_code_export(self):
        s = self.nouveau()
        s.source_runner.rc_datapump = {"expdp": ECHEC_DATAPUMP}
        self.assertEqual(s.aller(), ec.EXPORT)
        self.assertEqual(s.statut(11), FAILED)
        self.assertEqual(s.statut(12), "")

    def test_un_code_oracle_dans_la_sortie_est_un_echec(self):
        """Un code 0 n'exclut pas une erreur Oracle non fatale.

        C'est le piege principal de Data Pump : le client se termine
        proprement alors qu'un objet a echoue. Le controle porte sur les
        codes, jamais sur un texte traduit.
        """
        s = self.nouveau()
        s.source_runner.codes_oracle = {"expdp": "ORA-31641 ORA-31645"}
        self.assertEqual(s.aller(), ec.EXPORT)
        self.assertIn("ORA-31641", json.dumps(s.state.error, default=str))

    def test_un_code_avertissement_est_accepte(self):
        """Les codes 2, 4 et 8 sont des succes avec reserve.

        Les refuser rendrait l'outil inutilisable sur des schemas
        parfaitement sains comportant des objets que Data Pump signale
        sans pouvoir les traiter.
        """
        from osd.adapters.datapump import DATAPUMP_WARNING_CODES

        for rc in sorted(DATAPUMP_WARNING_CODES):
            with self.subTest(rc=rc):
                s = self.nouveau()
                s.source_runner.rc_datapump = {"expdp": rc}
                self.assertEqual(s.aller(), ec.SUCCESS)

    def test_un_job_d_export_en_erreur_est_lu_sur_la_source(self):
        """Le client peut se detacher en laissant tourner le job.

        Le code de sortie ne dit alors rien du resultat ; seul
        `DBA_DATAPUMP_JOBS` le dit. C'est la seule source qui Tage quand
        le client s'est detache.

        Le job d'export vit sur la **source**. Le lire sur la cible ne
        rendrait aucune ligne, et une absence de ligne se lit « aucun
        probleme » : le controle le plus destine a attraper un export
        detache serait donc le seul a ne rien voir.
        """
        s = self.nouveau(source=_adaptateur_source(
            job_state="FAILED", job_errors="7"))
        self.assertEqual(s.aller(), ec.EXPORT)
        self.assertIn("FAILED", json.dumps(s.state.error, default=str))

    def test_un_job_en_cours_est_un_echec(self):
        """Un job qui n'est pas termine n'a pas produit de dump complet.

        C'est l'etat du job, et non son compteur d'erreurs, qui porte
        cette information. Le compteur n'a de sens que sur un job ayant
        abouti, et il est absent de certaines vues
        `DBA_DATAPUMP_JOBS` : un job `FAILED` ou encore `RUNNING` dont
        le compteur n'a pas pu etre lu passerait alors pour un succes.

        L'inverse doit rester vrai : un compteur d'erreurs nul ne
        rattrape pas un etat qui ne l'est pas.
        """
        for etat, description in (
            ("RUNNING", "le client s'est detache et le job tourne encore"),
            ("FAILED", "le job a echoue"),
            ("STOPPED", "le job a ete interrompu"),
            ("NEEDS_COMMIT", "le job attend un commit"),
        ):
            with self.subTest(etat=etat):
                s = self.nouveau(source=_adaptateur_source(
                    job_state=etat, job_errors="0"))
                self.assertEqual(s.aller(), ec.EXPORT, description)
                self.assertIn(etat, json.dumps(s.state.error, default=str))

    def test_un_etat_de_job_absent_ne_fait_pas_echouer(self):
        """Ne pas connaitre l'etat du job n'est pas un echec d'export.

        L'etat du job est un controle **complementaire** : une session
        sans droit sur le dictionnaire, ou une vue absente, ne doit pas
        empecher une duplication parfaitement valide. C'est l'absence
        d'information qui est toleree, pas l'etat defavorable.
        """
        s = self.nouveau(source=_adaptateur_sans_job())
        self.assertEqual(s.aller(), ec.SUCCESS)



    def test_un_export_sans_fichier_produit_est_un_echec(self):
        """Un succes sans fichier n'est pas un succes.

        Data Pump peut se terminer sans erreur et sans rien ecrire si le
        repertoire n'est pas accessible en ecriture. L'import echouerait
        alors sur un fichier absent, trois etapes plus loin.
        """
        s = self.nouveau()
        s.source_runner.parties = []
        self.assertEqual(s.aller(), ec.EXPORT)
        self.assertIn("aucun fichier", s.state.error["message"])

    def test_un_repertoire_de_dump_illisible_est_un_echec(self):
        s = self.nouveau()
        s.source_runner.listdir_rc = 1
        self.assertEqual(s.aller(), ec.EXPORT)
        self.assertIn("repertoire", s.state.error["message"])

    def test_les_parties_sont_discoveryes_et_non_devinees(self):
        """Avec `PARALLEL > 1`, le nombre de parties n'est pas devinable.

        Le jeton `%d` ne garantit pas une serie complete : seul
        l'enumeration du repertoire dit ce qui a ete produit.
        """
        s = self.nouveau(config={"PARALLEL": "4"},
                         source_runner=FauxRunner(
                             label="source",
                             parties=("osd_R1-1.dmp", "osd_R1-2.dmp",
                                      "osd_R1-3.dmp"),
                         ))
        s.aller()
        noms = [p["name"] for p in s.state.artifacts["dump_parts"]]
        self.assertEqual(len(noms), 3)
        self.assertEqual(s.state.artifacts["dumpfile_spec"], "osd_R1-%d.dmp")

    def test_un_remap_de_tablespace_atteint_le_parfile(self):
        """Le remap doit se retrouver dans les options, pas seulement
        dans l'etat.

        Un remap valide cote configuration mais absent du parfile
        echouerait a l'import avec un message qui ne parle que du
        tablespace cible.
        """
        s = self.nouveau(config={"REMAP_TABLESPACE": "USERS:UTILITY"})
        s.aller()
        script = "".join(s.source_runner.scripts_de("datapump")).lower()
        # Data Pump est insensible a la casse sur les mots-cles de parfile,
        # et le parfile produit ici est en minuscules. Chercher la forme
        # majuscule testerait la casse du generateur, pas la presence du
        # remap -- un test qui echouerait sur un detail sans consequence
        # et passerait sur un remap reellement absent.
        self.assertIn("remap_tablespace=users:utility", script)


# --------------------------------------------------------------------------
# Transfert
# --------------------------------------------------------------------------

class TestTransfert(CasDeTest):
    def _bloquer_le_backend(self, erreur: Exception) -> None:
        """Remplace `TransferBackend.run` par un double en echec.

        Le remplacement est **global** a la classe, donc restaure par
        `addCleanup` : une fuite ferait passer les tests suivants avec
        un backend qui echoue toujours, et l'ordre d'execution deviendrait
        significant.
        """
        original = TransferBackend.run

        def _echec(self, *, src_dir, dst_dir, names, job_name):
            raise erreur

        TransferBackend.run = _echec
        self.addCleanup(setattr, TransferBackend, "run", original)

    def test_un_transfert_echoue_rend_le_code_transfert(self):
        s = self.nouveau(only=[13], config={"TRANSFER_MODE": "AUTO"})
        s.artefacts_de_datapump()
        self._bloquer_le_backend(
            TransferError("transfert impossible", detail=["espace disque insuffisant"])
        )
        self.assertEqual(s.aller(), ec.TRANSFER)
        self.assertEqual(s.statut(13), FAILED)
        # Les etapes suivantes n'ont **aucune entree** dans l'etat, et
        # non un statut `skipped` : `execute()` rend la main sur la
        # premiere erreur. C'est ce que `next_pending` utilise pour
        # reprendre exactement au bon endroit, et une entree `skipped`
        # ici la ferait passer a l'etape 14.
        self.assertEqual(s.statut(14), "")
        self.assertEqual(s.state.next_pending(list(range(1, 20))), 13)

    def test_un_repertoire_partage_ne_transfert_rien(self):
        """Les deux repertoires sont le meme : rien a copier.

        Le rapport doit le dire par la **topologie** (`partage`), et non
        par un detail technique comme une option SSH : ce sont deux
        natures, et confondre les deux avait produit un rapport ou « le
        dump a transite par le serveur de saut » et « ConnectTimeout
        vaut 30 » se lisaient de la meme facon.
        """
        s = self.nouveau()
        s.aller()
        transfert = s.state.artifacts["transfer"]
        self.assertEqual(transfert["method"], "partage")
        self.assertEqual(transfert["backend"], "local")

    def test_une_topologie_mixte_est_refusee(self):
        """Le filet de securite, quand la validation a ete contournee.

        La configuration refuse la topologie mixte a l'etape 2. Ce test
        appelle l'etape 13 directement pour verifier que la couche de
        transfert ne pretend pas, elle non plus, que le dump est deja
        visible de l'autre cote.
        """
        s = self.nouveau(only=[13], config={"TRANSFER_MODE": "AUTO"})
        s.source_runner.kind = "local"
        s.artefacts_de_datapump()
        with self.assertRaises(OsdError) as ctx:
            s.pipeline._step_13()
        self.assertEqual(ctx.exception.code, ec.TRANSFER)
        self.assertIn("mixte", str(ctx.exception))

    def test_un_chemin_de_transfert_indetermine_rend_le_code_transfert(self):
        """Sans les deux chemins, il n'y a pas de transfert a decrire.

        Echouer ici plutot que de copier « quelque part » evite
        d'ecrire le dump a un endroit que l'import ne lira pas.
        """
        s = self.nouveau(only=[13], config={"TRANSFER_MODE": "AUTO"})
        s.artefacts_de_datapump()
        s.state.artifacts.pop("source_directory_path")
        s.state.artifacts.pop("target_directory_path")
        s.source_adapter = _adaptateur_source(directory_path="")
        s.target_adapter = _adaptateur_cible(directory_path="")
        s.pipeline._source_adapter = s.source_adapter
        s.pipeline._target_adapter = s.target_adapter
        s.state.artifacts["source_directory_path"] = ""
        s.state.artifacts["target_directory_path"] = ""
        with self.assertRaises(OsdError) as ctx:
            s.pipeline._step_13()
        self.assertEqual(ctx.exception.code, ec.TRANSFER)
        self.assertIn("indeterminate", str(ctx.exception))

    def test_aucune_partie_a_transferer_hors_simulation_est_un_echec(self):
        s = self.nouveau(only=[13], config={"TRANSFER_MODE": "AUTO"})
        s.artefacts_de_datapump()
        s.state.artifacts["dump_parts"] = []
        with self.assertRaises(OsdError) as ctx:
            s.pipeline._step_13()
        self.assertEqual(ctx.exception.code, ec.EXPORT)


# --------------------------------------------------------------------------
# Echec d'import et validation
# --------------------------------------------------------------------------

class TestEchecImport(CasDeTest):
    def test_un_job_d_import_en_erreur_est_lu_sur_la_cible(self):
        """Meme controle que l'export, mais du bon cote.

        L'import se deroule sur la cible : c'est la qu'est le job. Le
        lire sur la source donnerait la meme absence de ligne que
        l'inverse, et le meme faux succes.
        """
        s = self.nouveau(cible=_adaptateur_cible(
            job_state="FAILED", job_errors="7"))
        self.assertEqual(s.aller(), ec.IMPORT)
        self.assertIn("FAILED", json.dumps(s.state.error, default=str))

    def test_un_import_en_echec_rend_le_code_import(self):
        s = self.nouveau()
        s.target_runner.rc_datapump = {"impdp": ECHEC_DATAPUMP}
        self.assertEqual(s.aller(), ec.IMPORT)
        self.assertEqual(s.statut(14), FAILED)
        self.assertEqual(s.statut(15), "")

    def test_un_code_oracle_a_l_import_est_remonte(self):
        s = self.nouveau()
        s.target_runner.rc_datapump = {"impdp": 0}
        s.target_runner.codes_oracle = {"impdp": "ORA-39000 ORA-31641"}
        self.assertEqual(s.aller(), ec.IMPORT)
        self.assertIn("ORA-39000", json.dumps(s.state.error, default=str))

    def test_un_dump_incomplet_est_un_echec_d_export_pas_d_import(self):
        """La relecture se fait sur la source, et l'import n'a pas commence.

        Rendre ce cas « echec de l'import » enverrait l'exploitant
        reexecuter un import alors que le remede est de **re-exporter** :
        le dump est tronque, il n'est pas incompatible.
        """
        s = self.nouveau()
        s.source_runner.rc_datapump = {"impdp": ECHEC_DATAPUMP}
        s.source_runner.codes_oracle = {"impdp": "ORA-39059 ORA-39246"}
        self.assertEqual(s.aller(), ec.EXPORT)
        self.assertEqual(s.statut(11), DONE)
        self.assertEqual(s.statut(12), FAILED)
        self.assertIn("ORA-39059", json.dumps(s.state.error, default=str))

    def test_un_objet_deja_present_a_un_remede_qui_le_nomme(self):
        """`ORA-31684` n'a pas a etre investigue : il est previsible.

        C'est le cas « objet existant », verifie sur une vraie 19c :
        l'import s'arrete sur une sequence deja presente, et le remede
        generique — « consulter le journal sur l'hote » — laisse
        l'exploitant chercher une cause qu'il a deja sous les yeux. Le
        remede doit dire ce que `TABLE_EXISTS_ACTION` ne couvre pas,
        puisque c'est la seule chose qu'il ne sait pas faire.
        """
        s = self.nouveau(config={"ALLOW_EXISTING_TARGET": "true"})
        s.target_runner.rc_datapump = {"impdp": 5}
        s.target_runner.codes_oracle = {"impdp": "ORA-31684 ORA-39111"}
        self.assertEqual(s.aller(), ec.IMPORT)
        remede = s.state.error.get("hint", "")
        self.assertNotIn("Consulter le journal", remede)
        self.assertIn("TABLE_EXISTS_ACTION", remede)
        self.assertIn("TABLES", remede)

    def test_un_code_inconnu_laisse_le_remede_generique(self):
        """Une remediation inventee est pire que la neutrality.

        Elle oriente l'exploitant vers une cause fausse, avec
        l'autorite que donne un remede specifique. Un code sans entree
        doit donc retomber sur la mention generique, qui renvoie au
        journal — la seule source qui dise vraiment.
        """
        s = self.nouveau()
        s.target_runner.rc_datapump = {"impdp": ECHEC_DATAPUMP}
        s.target_runner.codes_oracle = {"impdp": "ORA-99999"}
        self.assertEqual(s.aller(), ec.IMPORT)
        remede = s.state.error.get("hint", "")
        self.assertIn("Consulter le journal", remede)
        self.assertNotIn("TABLE_EXISTS_ACTION", remede)

    def test_ora_39111_seul_ne_designe_pas_une_cause(self):
        """Il annonce l'arret, pas la raison.

        `ORA-39111` suit `ORA-31684` sur un import arrete en cours de
        route. Le nommer seul comme cause ferait croire a un probleme
        de transactions, alors que le remede est de lire le code qui le
        precede — ce que le remede dit.
        """
        s = self.nouveau()
        s.target_runner.rc_datapump = {"impdp": 5}
        s.target_runner.codes_oracle = {"impdp": "ORA-39111"}
        self.assertEqual(s.aller(), ec.IMPORT)
        remede = s.state.error.get("hint", "")
        self.assertIn("precede", remede)
        self.assertNotIn("TABLE_EXISTS_ACTION", remede)

    def test_un_dump_relu_est_signale_comme_tel(self):
        s = self.nouveau()
        s.aller()
        self.assertTrue(s.state.artifacts["dump_verified"])

    def test_des_objets_invalides_apres_import_sont_signales(self):
        """Un objet invalide indique une dependance non satisfaite.

        `STANDARD` echoue quand meme : un objet invalide apres un import
        reussi n'est pas un detail, c'est un schema qui ne compiles pas
        cote cible.
        """
        s = self.nouveau(
            config={"ALLOW_EXISTING_TARGET": "true"},
            cible=_adaptateur_cible(object_count={"": 30, "INVALID": 3}),
        )
        self.assertEqual(s.aller(), ec.VALIDATION)
        self.assertEqual(s.statut(15), FAILED)
        self.assertIn("3", s.state.error["message"])

    def test_validation_full_accepte_des_invalides_avec_un_bilan(self):
        s = self.nouveau(
            config={"VALIDATION_LEVEL": "FULL",
                    "ALLOW_EXISTING_TARGET": "true"},
            cible=_adaptateur_cible(object_count={"": 30, "INVALID": 3}),
        )
        s.aller()
        self.assertEqual(s.statut(15), DONE)
        self.assertIn("invalide", s.etape(15).message)

    def test_un_schema_cible_disparu_est_un_code_validation(self):
        """Un `DROP USER` pendant l'import ne doit pas passer pour un succes.

        Le dump serait complet, l'import aurait rendu 0, et la cible
        serait vide : exactement le scenario que la comparaison des
        etapes 15 et 16 existe pour empecher.
        """
        cible = SchemaQuiDisparait(
            object_count=0, schema_exists=True,
            directory_path="/donnees/cible",
            tablespaces_capacity={"USERS": {"free_bytes": 100 * GIB},
                                 "UTILITY": {"free_bytes": 100 * GIB}},
            responses={"session_privs": [["IMP_FULL_DATABASE"]]},
        )
        s = self.nouveau(cible=cible)
        self.assertEqual(s.aller(), ec.VALIDATION)
        self.assertEqual(s.statut(15), FAILED)
        self.assertIn("n'existe plus", s.state.error["message"])
        # Le double suppose deux consultations, une a l'etape 7 et une a
        # la validation. L'annoncer evite que le test passe encore si
        # l'outil cessait d'en consulter une -- auquel cas le defaut
        # qu'il doit attraper ne serait plus detectable.
        self.assertEqual(cible.vues, 2)

    def test_un_inventaire_cible_incomplet_est_un_code_validation(self):
        """Comparer apres import est la seule preuve de la duplication.

        Un import « reussi » qui n'a pas recree tous les objets laisse une
        cible silencieusement amputee ; sans comparaison, le rapport
        serait favorable.
        """
        s = self.nouveau(
            source=_adaptateur_source(responses={
                "session_privs": [["EXP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "EMPLOYEES"], ["INDEX", "IX1"],
                                ["VIEW", "V1"]],
            }),
            cible=_adaptateur_cible(responses={
                "session_privs": [["IMP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "EMPLOYEES"]],
            }),
        )
        self.assertEqual(s.aller(), ec.VALIDATION)
        self.assertEqual(s.statut(16), FAILED)
        ecart = s.state.metrics["comparison"]
        self.assertIn("INDEX~IX1", ecart["missing_in_target"])
        self.assertIn("VIEW~V1", ecart["missing_in_target"])

    def test_un_objet_supplementaire_cible_est_signale_mais_non_bloquant(self):
        """Un objet en plus n'est pas une duplication ratee.

        La cible peut legitimement contenir davantage -- tables creees
        depuis. Seule l'absence bloque, et c'est ce que le test verifie.
        """
        s = self.nouveau(
            source=_adaptateur_source(responses={
                "session_privs": [["EXP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "EMPLOYEES"]],
            }),
            cible=_adaptateur_cible(responses={
                "session_privs": [["IMP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "EMPLOYEES"], ["TABLE", "AJOUT"]],
            }),
        )
        self.assertEqual(s.aller(), ec.SUCCESS)
        self.assertEqual(
            s.state.metrics["comparison"]["extra_in_target"], ["TABLE~AJOUT"]
        )

    def test_la_signature_distingue_type_et_nom(self):
        """Une table et un index de meme nom sont deux objets.

        Une signature reduite au nom ferait passer une table pour un
        index, et l'inventaire paraitrait reconcilie.
        """
        s = self.nouveau(
            source=_adaptateur_source(responses={
                "session_privs": [["EXP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "T"], ["INDEX", "T"]],
            }),
            cible=_adaptateur_cible(responses={
                "session_privs": [["IMP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "T"]],
            }),
        )
        self.assertEqual(s.aller(), ec.VALIDATION)
        self.assertIn("INDEX~T", s.state.metrics["comparison"]["missing_in_target"])

    def test_l_adaptateur_est_choisi_sur_le_nom_du_schema(self):
        """Les deux connexions sont souvent identiques.

        Un choix par position interrogerait la cible pour la source des
        lors que `SOURCE_CONNECT == TARGET_CONNECT` -- le cas le plus
        courant, et celui ou l'erreur resterait invisible parce que les
        reponses se ressemblent.
        """
        s = self.nouveau(
            source=_adaptateur_source(responses={
                "session_privs": [["EXP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "EMPLOYEES"]],
            }),
            cible=_adaptateur_cible(responses={
                "session_privs": [["IMP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "EMPLOYEES"]],
            }),
        )
        s.aller()
        self.assertEqual(s.aller(), ec.SUCCESS)
        # Les deux requetes portent leur schema : c'est la seule preuve
        # que le bon adaptateur a ete interroge.
        for adaptateur, schema in ((s.source_adapter, "SRC"),
                                   (s.target_adapter, "TGT")):
            self.assertTrue(
                any(schema in q for q in adaptateur.queries),
                f"aucune requete sur {schema}",
            )


# --------------------------------------------------------------------------
# Interruption
# --------------------------------------------------------------------------

class TestInterruption(CasDeTest):
    def _interrompre_a_l_import(self, s: Scenario) -> None:
        """Interrompt le run au premier `impdp` de la cible.

        C'est l'etape 14 — le moment ou un operateur arrete reellement
        un run : l'export est termine, le dump existe, l'import est en
        cours. Le seuil est 1 et non 2 parce que `impdp` ne tourne
        qu'une fois sur la cible : l'etat du job, lui, passe par
        `sqlplus`, pas par le corps Data Pump.
        """
        original = s.target_runner._reponse_datapump

        def _coupure(script):
            raise KeyboardInterrupt

        s.target_runner._reponse_datapump = _coupure

    def test_un_controle_c_pose_le_code_interruption(self):
        """Ctrl-C donne 9, et l'etape en cours est **marquee en echec**.

        La marquer est ce qui rend la reprise possible : sans statut, le
        `resume` ne saurait pas quelle etape relancer.
        """
        s = self.nouveau()
        self._interrompre_a_l_import(s)
        self.assertEqual(s.aller(), ec.INTERRUPTED)
        self.assertEqual(s.statut(14), FAILED)
        self.assertEqual(s.state.final_code, ec.INTERRUPTED)

    def test_l_interruption_laisse_un_etat_reprenable(self):
        """Ce qui est discriminant : les etapes anterieures sont terminees.

        Reprises telles quelles, elles ne seront pas rejouees : un
        `resume` qui relancerait l'export d'un schema de plusieurs
        giga-octets ferait perdre des heures pour rien.
        """
        s = self.nouveau()
        self._interrompre_a_l_import(s)
        s.aller()
        for index in range(1, 14):
            self.assertEqual(s.statut(index), DONE, f"etape {index}")
        self.assertTrue(s.state.is_resumable(list(range(1, 20))))

    def test_le_rapport_d_un_run_interrompu_est_construit(self):
        """Un rapport est ecrit meme en cas d'interruption.

        C'est lui qui dit ou le run en etait, donc ce qui reste a
        reprendre. Un rapport ecrit uniquement en cas de succes
        laisserait l'exploitant sans aucun etat apres un arret.
        """
        from osd.report import render_text

        s = self.nouveau()
        self._interrompre_a_l_import(s)
        s.aller()
        self.assertIn(ec.label(ec.INTERRUPTED), render_text(s.rapport()))

    def test_une_interruption_ne_laisse_pas_le_code_de_succis(self):
        """L'oubli classique : l'etape est marquee, le code final reste 0.

        L'exploitant voit alors un cron vert et un schema vide.
        """
        s = self.nouveau()
        self._interrompre_a_l_import(s)
        s.aller()
        self.assertNotEqual(s.state.final_code, ec.SUCCESS)
        self.assertNotEqual(s.state.final_code, None)

    def test_le_nettoyage_est_saute_apres_interruption(self):
        """Supprimer le dump apres un arret empecherait la reprise.

        Le dump est la seule chose qui permette de relancer l'import
        sans refaire l'export -- et c'est precisement ce que fait
        l'interruption, qu'on relance en general dans l'heure.

        Un operateur qui interrompt puis relance ne doit pas decouvrir
        que l'etape 18 a supprime le fichier qu'il lui fallait. L'etape
        n'est donc pas atteinte du tout, comme apres un echec.
        """
        s = self.nouveau(config={"CLEANUP_AFTER_SUCCESS": "true"})
        self._interrompre_a_l_import(s)
        self.assertEqual(s.aller(), ec.INTERRUPTED)
        self.assertEqual(s.statut(18), "")
        retraits = [sc for sc in s.mutations_reelles() if "unlink" in sc]
        self.assertEqual(retraits, [], "le dump a ete supprime malgre l'arret")


# --------------------------------------------------------------------------
# Reprise
# --------------------------------------------------------------------------

class TestReprise(CasDeTest):
    def test_les_etapes_deja_validees_ne_sont_pas_rejouees(self):
        s = self.nouveau(resume=True)
        s.deja_avance(12)
        s.artefacts_de_datapump()
        s.aller()
        # L'export n'a pas ete rejoue : c'est toute la raison d'etre de
        # l'etat. Un `resume` qui re-exporte fait perdre des heures.
        self.assertEqual(s.source_runner.scripts_de("datapump"), [])
        self.assertEqual(s.statut(11), DONE)

    def test_les_etapes_non_validees_sont_rejouees(self):
        s = self.nouveau(resume=True)
        s.deja_avance(12)
        s.artefacts_de_datapump()
        s.aller()
        # L'import, lui, n'a pas ete valide : il doit avoir eu lieu.
        self.assertTrue(s.target_runner.scripts_de("datapump"))

    def test_force_rejoue_meme_une_etape_validee(self):
        s = self.nouveau(resume=True, force=True)
        s.deja_avance(12)
        s.artefacts_de_datapump()
        s.aller()
        self.assertTrue(s.source_runner.scripts_de("datapump"))

    def test_une_reprise_reussie_nettoie_comme_un_run_reussi(self):
        """Le dump est preserve pour etre repris, puis supprime.

        Une interruption laisse le dump en place, et c'est exactement ce
        qui rend la reprise possible. Une fois celle-ci reussie, ce dump
        n'est plus la seule voie de recovery et doit partir comme
        apres n'importe quel succes.

        Le nettoyage lisait `state.final_code`, **relu dans le fichier
        d'etat** : il voyait donc l'interruption de la tentative
        precedente, sautait le nettoyage, et le rapport annoncait
        « echec anterieur » pour un run qui s'achevait sur un succes.
        Verifie sur une 19c reelle : 5 artefacts laissant 416 Ko de
        dump dans le DIRECTORY apres un `resume` reussi.
        """
        s = self.nouveau(resume=True, config={"CLEANUP_AFTER_SUCCESS": "true"})
        s.deja_avance(12)
        s.artefacts_de_datapump()
        # L'etat que la reprise relit : la tentative anterieure s'est
        # terminee sur une interruption.
        s.state.final_code = ec.INTERRUPTED
        s.aller()
        self.assertEqual(s.statut(18), DONE)
        self.assertIn("supprime", s.etape(18).message)
        retraits = [sc for sc in s.source_runner.mutations() if "unlink" in sc]
        self.assertTrue(retraits, "le dump de la reprise n'a pas ete nettoye")

    def test_un_echec_de_cette_invocation_conserve_encore_le_dump(self):
        """Le symetrique : la protection du dump ne doit pas disparaitre.

        Le nettoyage est subordonne a l'echec **de ce run**, pas a celui
        d'un etat sur disque. Une reprise qui echoue encore doit donc
        laisser le dump, sans quoi la reprise suivante n'a plus rien a
        rejouer — le cas que la regle protegeait reellement.
        """
        s = self.nouveau(resume=True, config={"CLEANUP_AFTER_SUCCESS": "true"})
        s.deja_avance(12)
        s.artefacts_de_datapump()
        s.state.final_code = ec.INTERRUPTED
        s.target_runner.rc_datapump = {"impdp": ECHEC_DATAPUMP}
        self.assertEqual(s.aller(), ec.IMPORT)
        self.assertEqual(s.statut(18), "")

    def test_le_rapport_ne_vend_pas_comme_faites_les_etapes_reprises(self):
        """Une etape non rejouee ne peut pas s'afficher comme reussie.

        Une reprise qui echoue a l'etape 14 affichait les etapes 15 a 19
        avec le statut de la tentative reussie precedente — y compris
        « 19. retourner-code : code 0 » sur un processus qui rendait 6.
        Le journal s'arretait bien a 14 ; c'est le rapport, lu en premier,
        qui mentait.

        L'etat doit donc distinguer « validee » — ce qu'un resume peut
        sauter — de « executee par ce run », ce qu'il doit montrer.
        """
        from osd.report import render_text

        s = self.nouveau(resume=True)
        # Une tentative anterieure avait valide 1 a 13 **et** 15 a 19 ;
        # c'est ce que la reprise relit. L'etape 14 n'y est pas, et
        # c'est elle que ce run rejoue et perd.
        s.marquer_validees(list(range(1, 14)) + list(range(15, 20)))
        s.artefacts_de_datapump()
        s.target_runner.rc_datapump = {"impdp": ECHEC_DATAPUMP}
        self.assertEqual(s.aller(), ec.IMPORT)

        par_index = {st["index"]: st for st in s.rapport()["steps"]}
        for index in list(range(1, 14)) + list(range(15, 20)):
            with self.subTest(etape=index):
                self.assertTrue(par_index[index]["carried_over"],
                                f"etape {index} non marquee comme reprise")
        self.assertFalse(par_index[14]["carried_over"],
                         "l'etape rejouee est marquee comme reprise")
        texte = render_text(s.rapport())
        self.assertIn("REPRISE", texte)
        self.assertNotIn("[OK   ] 19.", texte)

    def test_le_document_jsonporte_la_distinction(self):
        """`--json` doit dire la meme chose que le texte.

        Le JSON est la sortie d'un ordonnanceur : une etape reprise y
        doit etre identifiable sans passer par le rendu. Si le drapeau
        n'etait reproduit que dans le texte, l'automatisation verrait un
        run complet la ou l'exploitant voit un echec a l'etape 14.
        """
        s = self.nouveau(resume=True)
        s.marquer_validees(list(range(1, 14)) + list(range(15, 20)))
        s.artefacts_de_datapump()
        s.aller()
        etapes = {e["index"]: e for e in s.rapport()["steps"]}
        self.assertTrue(etapes[1]["carried_over"])
        self.assertFalse(etapes[14]["carried_over"])

    def test_sans_resume_tout_est_rejoue(self):
        """`resume` n'est pas un mode : c'est une decision.

        Sans lui, un etat existant est ignore. C'est le comportement
        attendu d'un `run` ordonne par cron, ou l'etat de la veille ne
        doit pas conditionner le run du jour.
        """
        s = self.nouveau(resume=False)
        s.deja_avance(12)
        s.aller()
        self.assertTrue(s.source_runner.scripts_de("datapump"))

    def test_un_etat_sans_etapes_validees_ne_bloque_rien(self):
        s = self.nouveau(resume=True)
        s.aller()
        self.assertTrue(s.source_runner.scripts_de("datapump"))

    def test_l_etat_survit_a_un_rechargement_depuis_le_disque(self):
        """L'etat sert a un **autre processus**.

        Le `resume` est lance par un crontab, donc par un processus
        distinct de celui qui a echoue. Un etat qui ne se relit pas
        depuis le disque n'aurait de valeur que dans le processus qui
        l'a produit -- c'est-a-dire jamais.
        """
        s = self.nouveau()
        s.aller()
        chemin = self.racine / "work" / "state.json"
        StateStore(chemin).save(s.state)

        relu = StateStore(chemin).load()
        self.assertIsNotNone(relu)
        self.assertEqual(relu.run_id, s.state.run_id)
        self.assertEqual(relu.steps[11].status, DONE)
        self.assertEqual(relu.final_code, ec.SUCCESS)

    def test_la_reprise_dans_un_autre_processe_saute_l_export(self):
        """Le cas reel du `resume` : l'etat vient d'un run anterieur.

        Le scenario est donc reconstruit a partir de l'etat relu, avec
        des runners neufs. C'est exactement ce que fait le `resume` d'un
        crontab, et c'est la seule facon de prouver que l'etat suffit.
        """
        s = self.nouveau()
        s.target_runner.rc_datapump = {"impdp": ECHEC_DATAPUMP}
        s.aller()
        self.assertEqual(s.state.final_code, ec.IMPORT)
        chemin = self.racine / "work" / "state.json"
        StateStore(chemin).save(s.state)

        # Seule l'etape 14 est a rejouer. Effacer 11, 12 et 13 simulerait
        # un etat ou **rien n'a ete exporte** : la reprise rejouerait alors
        # l'export, ce qui serait correct, et le test ne testerait plus la
        # reprise.
        relu = StateStore(chemin).load()
        relu.steps[14].status = "pending"
        relu.final_code = None
        StateStore(chemin).save(relu)

        suivant = Scenario(racine=self.racine, resume=True, config={
            "SOURCE_HOST": "source.exemple", "TARGET_HOST": "cible.exemple",
            "LOG_DIR": str(self.racine / "logs"),
            "WORK_DIR": str(self.racine / "work"),
            "REPORT_DIR": str(self.racine / "reports"),
        })
        suivant.pipeline.state = relu
        self.assertTrue(relu.is_resumable(list(range(1, 20))))
        self.assertEqual(suivant.aller(), ec.SUCCESS)
        # L'export ne doit pas etre rejoue : c'est lui qui coute des
        # heures. L'import, lui, doit l'etre.
        self.assertEqual(
            suivant.source_runner.scripts_de("datapump"), [],
            "l'export a ete rejoue alors que l'etat le donnait pour fait",
        )
        self.assertTrue(suivant.target_runner.scripts_de("datapump"))

    def test_un_echec_d_export_n_est_pas_rejouable_automatiquement(self):
        """Rejouer un export rate sans `--force` Infinite la boucle.

        Un echec d'export est presque toujours durable — droits,
        tablespace, reseau. Le rejouer toutes les dix minutes depuis un
        crontab remplirait le disque de logs sans rien reussir.
        """
        s = self.nouveau(resume=True)
        s.state.start(11, STEP_NAMES[11])
        s.state.finish(11, STEP_NAMES[11], status=FAILED, code=ec.EXPORT,
                       message="echec")
        s.source_runner.rc_datapump = {"expdp": ECHEC_DATAPUMP}
        self.assertEqual(s.aller(), ec.EXPORT)

    def test_force_rejoue_un_echec_d_export(self):
        s = self.nouveau(resume=True, force=True)
        s.state.start(11, STEP_NAMES[11])
        s.state.finish(11, STEP_NAMES[11], status=FAILED, code=ec.EXPORT,
                       message="echec")
        s.aller()
        self.assertTrue(s.source_runner.scripts_de("datapump"))


# --------------------------------------------------------------------------
# Concurrence
# --------------------------------------------------------------------------

class TestConcurrence(CasDeTest):
    """Ce que le pipeline apporte au verrou — et rien d'autre.

    L'exclusion mutuelle elle-meme est verifiee dans `test_lock.py`, entre
    **deux processus** reels : c'est la seule maniere de l'eprouver, les
    verrous `fcntl` appartenant au processus, donc un second `lockf`
    pris depuis le meme processus reussit toujours. La recopier ici
    n'aurait ajoute aucune couverture et aurait ete fausse — les appels
    `acquire()` utilises n'existent pas, l'objet n'etant qu'un
    gestionnaire de contexte.

    Il ne reste qu'une propriete propre au pipeline : le verrou doit
    dependre du **couple de schemas**, et non de la machine.
    """

    def test_la_cle_de_verrou_depend_du_couple_de_schemas(self):
        """Deux duplications differentes ne doivent pas se bloquer.

        Un verrou global sur la machine rendrait l'outil inutilisable
        des qu'un second schema est duplique — et c'est le cas nominal
        pour un serveur de saut qui heberge plusieurs bases.
        """
        from osd.lock import lock_key

        a = support.load_config(SOURCE_SCHEMA="HR", TARGET_SCHEMA="UAT")
        b = support.load_config(SOURCE_SCHEMA="HR", TARGET_SCHEMA="DEV")
        self.assertNotEqual(lock_key(a), lock_key(b))
        self.assertEqual(
            lock_key(a),
            lock_key(support.load_config(SOURCE_SCHEMA="HR",
                                         TARGET_SCHEMA="UAT")),
        )


# --------------------------------------------------------------------------
# Le dry-run
# --------------------------------------------------------------------------

class TestSimulation(CasDeTest):
    def test_les_dix_neuf_etapes_sont_executees(self):
        """Un dry-run qui s'arretait tot ne validerait pas l'environnement.

        C'est tout l'interet du mode : savoir si la connexion aboutit, si
        le schema existe, s'il manque de la place — avant d'engager un
        export de plusieurs heures.
        """
        s = self.nouveau(dry_run=True)
        self.assertEqual(s.aller(), ec.SUCCESS)
        for index in range(1, 18):
            self.assertEqual(
                s.statut(index), DONE, f"etape {index:02d} : {s.statut(index)}"
            )
        # L'etape 18 est bien **executee** en simulation, et c'est son
        # statut `skipped` qui dit qu'aucun artefact n'a ete supprime :
        # un statut `done`+E cause 0 affirmation que la simulation ne peut
        # pas soutenir.
        self.assertEqual(s.statut(18), SKIPPED)
        self.assertEqual(s.etape(18).message, "simulation")

    def test_aucune_mutation_n_atteint_un_runner_reel(self):
        """La seule mesure qui ne depend d'aucune promesse du rapport.

        Le `NullRunner` retient les mutations, mais une mutation reelle
        passerait ailleurs — un appel direct a `subprocess`, un `if`
        oublie quelque part. Cette assertion est la garantie elle-meme,
        pas la consequence d'une lecture du code.
        """
        s = self.nouveau(dry_run=True)
        s.aller()
        self.assertEqual(
            s.mutations_reelles(), [],
            "une mutation a ete executee en simulation",
        )

    def test_les_mutations_retenues_sont_lisibles(self):
        """L'apercu est l'information premiere d'un dry-run.

        « Tout a ete verifie, rien n'a ete change » n'a de valeur que si
        l'on peut voir *quoi* n'a pas ete change. Un rapport qui dit
        seulement « rien » ne permet aucune revue.
        """
        s = self.nouveau(dry_run=True)
        s.aller()
        retenues = s.pipeline.withheld_mutations()
        self.assertTrue(retenues, "aucune mutation retenue : l'apercu est vide")
        joint = "\n".join(retenues)
        self.assertIn("expdp", joint)
        self.assertIn("impdp", joint)

    def test_les_mutations_retenues_sont_cotees(self):
        """Le prefixe de cote est ce qui rend l'apercu exploitable.

        Une operation destructive sans dire sur quel schema serait
        inexploitable, et pourrait faire croire a une action sur la
        source.
        """
        s = self.nouveau(dry_run=True)
        s.aller()
        retenues = s.pipeline.withheld_mutations()
        self.assertTrue(any(r.startswith("[source]") for r in retenues))
        self.assertTrue(any(r.startswith("[cible]") for r in retenues))

    def test_les_lectures_sont_bien_reelles(self):
        """Un dry-run qui ne lirait rien ne prouverait rien.

        Le delegate recoit les lectures : c'est la difference entre
        « verifie » et « suppose ».
        """
        s = self.nouveau(dry_run=True)
        s.aller()
        self.assertTrue(s.reel_source.lectures())
        self.assertTrue(s.reel_cible.lectures())
        self.assertEqual(s.lectures_reelles(), s.reel_source.lectures()
                         + s.reel_cible.lectures())

    def test_le_dump_n_est_pas_relu_en_simulation(self):
        """`dump_verified` n'est pas pose : il ne peut designe qu'un fait reel.

        Un `resume` qui le lirait croirait a une garantie que la
        simulation n'a pas produite.
        """
        s = self.nouveau(dry_run=True)
        s.aller()
        self.assertNotIn("dump_verified", s.state.artifacts)

    def test_la_cible_n_est_pas_importee_en_simulation(self):
        s = self.nouveau(dry_run=True)
        s.aller()
        self.assertNotIn("imported", s.state.artifacts)

    def test_le_nettoyage_est_saute_en_simulation(self):
        s = self.nouveau(dry_run=True, config={"CLEANUP_AFTER_SUCCESS": "true"})
        s.aller()
        self.assertEqual(s.statut(18), SKIPPED)
        self.assertEqual(s.mutations_reelles(), [])

    def test_l_ecart_d_inventaire_n_est_pas_un_echec_en_simulation(self):
        """La cible n'a pas ete modifiee : l'ecart est l'etat de depart.

        Le signaler comme un echec de validation ferait conclure que
        l'outil est casse alors qu'il n'a rien fait. Un faux positif ici
        est particulierement destructeur : il fait abandonner l'outil.
        """
        s = self.nouveau(
            dry_run=True,
            cible=_adaptateur_cible(responses={
                "session_privs": [["IMP_FULL_DATABASE"]],
                "dba_objects": [["TABLE", "EMPLOYEES"]],
            }),
        )
        self.assertEqual(s.aller(), ec.SUCCESS)
        self.assertIn("avant", s.etape(16).message)

    def test_un_schema_cible_inexistant_est_refuse_dans_les_deux_modes(self):
        """L'outil ne cree pas de schema : c'est le travail de la base.

        Le refus doit etre identique en simulation, sinon le dry-run
        validerait une configuration que l'execution reelle refuserait --
        et c'est le pire defaut possible pour un mode de preparation.
        """
        s = self.nouveau(dry_run=True, cible=_adaptateur_cible(schema_exists=False))
        self.assertEqual(s.aller(), ec.PREREQ)
        self.assertEqual(s.statut(7), FAILED)

    def test_le_rapport_annonce_la_simulation(self):
        from osd.report import render_text

        s = self.nouveau(dry_run=True)
        s.aller()
        self.assertIn("simulation", render_text(s.rapport()).lower())

    def test_l_etat_porte_la_simulation(self):
        s = self.nouveau(dry_run=True)
        s.aller()
        self.assertTrue(s.state.dry_run)

    def test_les_scripts_retenus_sont_relisibles(self):
        """Le script complet part en piece jointe du rapport.

        Le resume ne porte que les arguments ; sans le script, une revue
        des options Data Pump imposerait de rejouer le run.
        """
        s = self.nouveau(dry_run=True)
        s.aller()
        retenu = s.pipeline.source_runner
        self.assertTrue(retenu.scripts)
        self.assertIn("expdp", "".join(retenu.scripts))

    def test_le_runner_reel_est_construit_meme_en_simulation(self):
        """Le `NullRunner` a besoin d'un delegate pour les lectures.

        Sans lui, le dry-run ne pourrait ni repondre sur les binaires ni
        mesurer l'espace : il produirait un rapport vide, donc inutile.
        Le dry-run ne supprime donc pas la construction du runner reel, il
        en limite l'usage — ce qui est la distinction utile.
        """
        s = self.nouveau(dry_run=True)
        s.aller()
        self.assertIsInstance(s.pipeline.source_runner, NullRunner)
        self.assertTrue(s.reel_source.lectures())

    def test_le_calcul_de_la_marge_est_identique_au_run_reel(self):
        """Le meme code s'execute : l'estimation ne peut pas diverger.

        Un dry-run qui afficherait une estimation differente de celle du
        run reel donnerait une confiance fausse sur l'espace disponible.
        """
        reel = self.nouveau()
        reel.aller()
        simule = self.nouveau(dry_run=True)
        simule.aller()
        self.assertEqual(
            reel.state.metrics["source_bytes"],
            simule.state.metrics["source_bytes"],
        )

    def test_une_option_destructive_est_refusee_en_simulation(self):
        """Le garde-fou ne s'echappe pas parce que rien ne sera ecrit.

        Un dry-run n'ecrit rien, mais il ne doit pas non plus valider une
        configuration que le run reel refuserait.
        """
        s = self.nouveau(dry_run=True, config={"TABLE_EXISTS_ACTION": "REPLACE"})
        self.assertEqual(s.aller(), ec.SECURITY)


# --------------------------------------------------------------------------
# Selection d'etapes et proprietes derivees
# --------------------------------------------------------------------------

class TestSelectionDetapes(CasDeTest):
    def test_only_eleve_des_etapes(self):
        s = self.nouveau(only=[1, 2])
        self.assertEqual(s.aller(), ec.SUCCESS)
        self.assertEqual(s.statut(1), DONE)
        self.assertEqual(s.statut(3), SKIPPED)

    def test_les_etapes_hors_perimetre_ne_passent_pas(self):
        s = self.nouveau(only=[1, 2])
        s.aller()
        self.assertEqual(s.source_runner.scripts_de("datapump"), [])
        self.assertEqual(s.source_runner.scripts_de("espace"), [])

    def test_un_perimetre_vide_passe_tout_en_hors_perimetre(self):
        s = self.nouveau(only=[])
        s.aller()
        for index in range(1, 20):
            self.assertEqual(s.statut(index), SKIPPED)

    def test_une_etape_non_implementee_echoue(self):
        """Le dispatch doit etre explicite sur ses limites.

        Silencieusement ne rien faire donnerait un rapport de succes
        pour une etape qui n'a pas existe.
        """
        s = self.nouveau()
        with self.assertRaises(ConfigError):
            s.pipeline._dispatch(99, "inconnue")


class TestProprietesDerivees(CasDeTest):
    def test_les_objets_d_acces_sont_memoises(self):
        """Un `OracleAdapter` porte un etat (`_verified`).

        Le reconstruire a chaque acces le ferait perdre, et
        re-testerait la connexion — donc un aller-retour SQL*Plus par
        appel, sur un schema de plusieurs heures.
        """
        s = self.nouveau()
        s.aller()
        self.assertIs(s.pipeline.source_runner, s.source_runner)
        self.assertIs(s.pipeline.source_adapter, s.source_adapter)
        self.assertIs(s.pipeline.target_adapter, s.target_adapter)

    def test_le_prefixe_du_job_derive_du_run(self):
        """Le nom de job doitporter le `run_id`.

        C'est lui qui distingue deux runs dans le dictionnaire
        Data Pump, et donc qui permet de retrouver le journal du bon
        run apres coup.
        """
        s = self.nouveau(config={"JOB_PREFIX": "OSD"})
        s.aller()
        self.assertEqual(s.state.artifacts["job_name"], "OSD_R1")

    def test_le_nom_de_job_est_borne_a_la_longueur_maximale(self):
        """Oracle refuse un nom de job de plus de 30 caracteres.

        Un prefixe long donne un nom invalide, et l'echec ne se voit
        qu'a l'export.
        """
        s = self.nouveau(config={"JOB_PREFIX": "P" * 40})
        s.aller()
        self.assertLessEqual(len(s.state.artifacts["job_name"]), 30)

    def test_le_spec_de_dump_suit_le_parallelisme(self):
        s = self.nouveau(config={"PARALLEL": "4"})
        s.aller()
        self.assertEqual(s.state.artifacts["dumpfile_spec"], "osd_R1-%d.dmp")
        s2 = self.nouveau(config={"PARALLEL": "1"})
        s2.aller()
        self.assertEqual(s2.state.artifacts["dumpfile_spec"], "osd_R1.dmp")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
