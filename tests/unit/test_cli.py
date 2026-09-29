"""Tests de l'interface en ligne de commande.

La CLI est la **seule** partie de l'outil que l'ordonnanceur voit
directement. C'est elle qui rend le code de retour, et un code de retour
faux est la seule panne qui ne se voit pas : le traitement a reussi, la
duplication a ete faite, et l'automate relancera indefiniment un travail
que personne ne regarde.

Trois proprietes sont donc verifiees ici, et elles ne se verifient nulle
part ailleurs :

* `main()` **ne leve jamais** et **ne rend jamais `None`**. Un `None`
  rendu a `sys.exit` vaut 0 : une erreur signalee sur stderr ferait
  alors croire a un succes a l'ordonnanceur. C'est le point sur lequel
  un test est le seul a pouvoir echouer, puisque les autres testent des
  retours nominaux.
* Le code rendu est l'un des dix codes documentes, et il est bien celui
  de l'etape qui a echoue.
* `stdout` ne porte que le rapport, `stderr` que les diagnostics. Un
  `osd run > sortie.txt` doit produire un rapport exploitable, pas un
  melange dont il faut deviner la partie utile.

Le pipeline est remplace par un double : ces tests portent sur la CLI, et
non sur la duplication. L'orchestration est verifiee dans
`test_pipeline.py`, la configuration dans `test_config.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

import support  # noqa: F401
from support import SRC_DIR

from osd import __version__
from osd import exit_codes as ec
from osd.errors import Interrupted, OsdError
from osd.lock import Lock
from osd.state import State


# --------------------------------------------------------------------------
# Double de pipeline
# --------------------------------------------------------------------------

class PipelineFaux:
    """Remplace `osd.stages.Pipeline`, sans base ni reseau.

    Il retient les arguments de construction — c'est par eux que l'on
    verifie ce que la CLI **demande**, sans se soucier de ce que le
    pipeline en fait — et rend un code choisi.

    `execute()` enregistre les dix-neuf etapes comme **reussies**, comme
    le ferait un run complet. C'est ce qui rend le rapport exploitable
    dans ces tests : un rapport sans aucune etape passerait tous les
    tests de forme — « il existe », « il est du JSON », « il ne fuit
    aucun secret » — en ne prouvant rien de ce qu'ils croient
    verifier.

    `lever` permet de declencher une exception depuis `execute()`, ce qui
    est la seule facon d'eprouver le filet de securite de `main()`.
    """

    #: Instances construites, dans l'ordre. Reset par `setUp`.
    instances: List["PipelineFaux"] = []

    #: Code rendu par `execute()`.
    code = ec.SUCCESS

    #: Exception levee par `execute()`, ou `None`.
    lever: Optional[BaseException] = None

    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.state: State = kw.get("state") or State(run_id="R")
        # Le vrai pipeline reporte le drapeau dans l'etat a la
        # construction (`pipeline.py`). Sans cela, le rapport dirait
        # « simulation : non » pour un run simule : le double mentirait,
        # et c'est le rapport que l'utilisateur lit.
        self.state.dry_run = bool(kw.get("dry_run"))
        self.checks: List[Any] = []
        self.execute_appele = False
        self.withheld: List[str] = ["script simule"]
        type(self).instances.append(self)

    def execute(self) -> int:
        from osd.state import DONE
        from osd.stages.pipeline import STEPS, STEP_NAMES

        self.execute_appele = True
        if type(self).lever is not None:
            raise type(self).lever
        for index, name in STEPS:
            self.state.start(index, name)
            self.state.finish(index, name, status=DONE, message="execute")
        self.state.final_code = type(self).code
        return type(self).code

    def withheld_mutations(self) -> List[str]:
        return self.withheld


def _installer_double() -> Any:
    """Remplace le pipeline, et rend la classe d'origine."""
    from osd import stages

    origine = stages.Pipeline
    PipelineFaux.instances = []
    PipelineFaux.code = ec.SUCCESS
    PipelineFaux.lever = None
    stages.Pipeline = PipelineFaux
    return origine


# --------------------------------------------------------------------------
# Harnais
# --------------------------------------------------------------------------

class CasDeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.origine = _installer_double()
        self.addCleanup(lambda: setattr(__import__("osd.stages", fromlist=["x"]),
                                       "Pipeline", self.origine))

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.racine = Path(self.tmp.name)
        self.base = self.racine / "base"

        # Un fichier de configuration minimal mais valide, ecrit en 0600 :
        # `config.load` signale en avertissement tout fichier lisible par
        # d'autres, et un avertissement dans le journal d'un test rend
        # la sortie illisible.
        self.config = self.racine / "config.conf"
        lignes = [
            "SOURCE_CONNECT=L_SRC",
            "TARGET_CONNECT=L_TGT",
            "SOURCE_SCHEMA=HR",
            "TARGET_SCHEMA=UAT",
            "SOURCE_DIRECTORY=DP_SRC",
            "TARGET_DIRECTORY=DP_TGT",
            f"LOG_DIR={self.base / 'logs'}",
            f"WORK_DIR={self.base / 'work'}",
            f"REPORT_DIR={self.base / 'reports'}",
            f"LOCK_DIR={self.base / 'locks'}",
        ]
        self.config.write_text("\n".join(lignes) + "\n", encoding="utf-8")
        self.config.chmod(0o600)

        # L'environnement est neutralise : une variable `OSD_*` presente
        # sur la machine de developpement changerait le resultat d'un test
        # sans qu'aucune assertion ne le montre.
        for cle in list(os.environ):
            if cle.startswith("OSD_"):
                self.addCleanup(os.environ.__setitem__, cle, os.environ[cle])
                del os.environ[cle]

    # -- Appels ----------------------------------------------------------

    def poser_etat(self, run_id: str, **kw: Any) -> Path:
        """Ecrit un etat de reprise valide, comme le ferait un run passe.

        Le fichier est produit par `StateStore` et non ecrit a la main :
        un etat forge a la main risque d'imposer au code de test une
        forme qu'aucun run reel ne produit, et le test passerait sur un
        etat que l'outil ne sait pas relire.
        """
        from osd.state import StateStore

        work = self.base / "work"
        work.mkdir(parents=True, exist_ok=True)
        etat = State(run_id=run_id, source_schema="HR", target_schema="UAT")
        etat.final_code = kw.pop("final_code", ec.SUCCESS)
        chemin = work / f"state-{run_id}.json"
        StateStore(chemin).save(etat)
        return chemin

    def _capturer(self) -> Any:
        sortie, erreurs = io.StringIO(), io.StringIO()
        return sortie, erreurs

    def main(self, *argv: str) -> int:
        """Appelle `main()` en capturant ses deux flux.

        Aucun `--quiet` automatique : le contrat « stdout ne porte que le
        rapport » se verifie justement parce que le journal **écrit**,
        et un test qui le supprimerait testerait un cas qui n'existe pas.
        """
        from osd.cli import main

        sortie, erreurs = self._capturer()
        with contextlib.redirect_stdout(sortie), contextlib.redirect_stderr(erreurs):
            code = main(list(argv))
        self.sortie = sortie.getvalue()
        self.erreurs = erreurs.getvalue()
        return code

    def run_cli(self, *argv: str) -> int:
        """Ajoute `-c` **apres** la sous-commande, ou elle est acceptee.

        `-c` est une option de sous-commande, pas une option globale :
        `osd -c fichier run` est refuse, `osd run -c fichier` est la
        forme reelle. Le reflected ici, parce que c'est l'erreur que
        l'on fait spontanement, et que le message d'`argparse` la
        formule tres bien.
        """
        return self.main(argv[0], "-c", str(self.config), *argv[1:])

    @property
    def pipeline(self) -> PipelineFaux:
        self.assertTrue(PipelineFaux.instances, "le pipeline n'a pas ete construit")
        return PipelineFaux.instances[-1]


# --------------------------------------------------------------------------
# Le contrat de sortie
# --------------------------------------------------------------------------

class TestContratDeSortie(CasDeTest):
    def test_main_ne_leve_jamais(self):
        """Une exception interne ne doit jamais sortir de `main()`.

        Le filet est large exprès : une `ValueError` dans une etape
        devient un code normalise, pas un traceback. Ce qui compte est le
        code, non la cause — la cause va dans le journal, ou l'exploitant
        la trouvera ; ce qui n'a pas de remede, c'est l'arret sur trace.
        """
        for erreur in (ValueError("bogue"), RuntimeError("bogue"),
                       KeyError("cle"), Interrupted("sigint")):
            with self.subTest(erreur=type(erreur).__name__):
                PipelineFaux.lever = erreur
                PipelineFaux.code = ec.SUCCESS
                code = self.run_cli("run")
                self.assertIsInstance(code, int)
                PipelineFaux.lever = None

    def test_main_ne_rend_jamais_none(self):
        """Un `None` rendu a `sys.exit` vaut 0 : succes automatique.

        C'est la panne la plus grave possible pour un outil commande par
        cron — tout echec signal deviendrait invisible. Le test est
        volontairement brutal : on force le pipeline a lever, puis on
        verifie que le code reste un entier.
        """
        PipelineFaux.lever = OsdError("echec", ec.EXPORT)
        code = self.run_cli("run")
        self.assertIsInstance(code, int, "main() a rendu None")
        self.assertNotIsInstance(code, type(None))
        self.assertEqual(code, ec.EXPORT)

    def test_une_erreur_d_usage_rend_le_code_configuration(self):
        """argparse sort avec 2, et 2 est deja « prerequis ».

        Sous cron, un `osd rn` mal orthographie se faisait lire comme un
        manque de privileges, et l'exploitant allait verifier des
        privileges au lieu de son orthographe.
        """
        code = self.run_cli("rn")
        self.assertEqual(code, ec.CONFIG)

    def test_une_option_inconnue_rend_le_code_configuration(self):
        self.assertEqual(self.run_cli("run", "--inexistant"), ec.CONFIG)

    def test_set_malforme_rend_le_code_configuration(self):
        self.assertEqual(self.run_cli("run", "--set", "PAS_EGAL"), ec.CONFIG)

    def test_help_rend_zero(self):
        """`--help` est un succes, pas une erreur.

        Le confondre romprait tout script qui interroge l'usage de
        l'outil pour verifier une version installee.
        """
        self.assertEqual(self.main("--help"), ec.SUCCESS)

    def test_version_rend_zero(self):
        self.assertEqual(self.main("--version"), ec.SUCCESS)
        self.assertIn(__version__, self.sortie)


# --------------------------------------------------------------------------
# Les codes
# --------------------------------------------------------------------------

class TestCodesDeRetour(CasDeTest):
    def test_les_dix_codes_sont_definis_et_nommes(self):
        """Un code sans libelle s'affiche en chiffre dans le rapport.

        L'exploitant qui lit « 6 » doit pouvoir savoir quoi faire sans
        ouvrir la documentation. Les libelles sont donc une donnee du
        contrat, pas un agrement d'affichage.
        """
        for code in range(10):
            with self.subTest(code=code):
                self.assertNotEqual(ec.label(code), "")

    def test_chaque_code_est_obtenu(self):
        """Chaque code est **atteignable**, pas seulement defini.

        Un code que rien ne produit est un code menteur dans la
        documentation : l'exploitant ecrira un traitement pour lui et
        attendra un evenement qui n'arrivera pas.
        """
        for code in range(10):
            with self.subTest(code=code):
                PipelineFaux.code = code
                PipelineFaux.lever = None
                self.assertEqual(self.run_cli("run"), code)

    def test_le_code_provient_du_pipeline(self):
        PipelineFaux.code = ec.VALIDATION
        self.assertEqual(self.run_cli("run"), ec.VALIDATION)
        self.assertEqual(self.state_run().final_code, ec.VALIDATION)

    def test_une_osd_error_transporte_son_code(self):
        PipelineFaux.lever = OsdError("tablespace absent", ec.PREREQ,
                                      detail=["USERS"], hint="creer le tablespace")
        self.assertEqual(self.run_cli("run"), ec.PREREQ)
        self.assertIn("tablespace absent", self.erreurs)
        self.assertIn("creer le tablespace", self.erreurs)

    def test_une_interruption_rend_le_code_interruption(self):
        PipelineFaux.lever = KeyboardInterrupt
        self.assertEqual(self.run_cli("run"), ec.INTERRUPTED)
        self.assertEqual(self.state_run().final_code, ec.INTERRUPTED)

    def test_une_erreur_interne_ne_rend_pas_zero(self):
        """Le filet de securite ne doit pas degrader une panne en succes.

        Rendre 0 serait le pire choix possible : la duplication a
        echoue, et l'ordonnanceur le rappellerait en croyant que la
        premiere tentative n'a pas abouti.
        """
        PipelineFaux.lever = ValueError("bogue interne")
        code = self.run_cli("run")
        self.assertNotEqual(code, ec.SUCCESS)
        self.assertIn("erreur interne", self.erreurs)

    def state_run(self) -> State:
        etats = list((self.base / "work").glob("state-*.json"))
        self.assertEqual(len(etats), 1, f"etats ecrits : {etats}")
        from osd.state import StateStore

        return StateStore(etats[0]).load()

    def test_l_etat_est_ecrit_meme_en_echec(self):
        """Sans etat, la reprise n'a rien sur quoi travaille.

        C'est le point verifie ici : le fichier doit exister et porter le
        code d'echec, sinon `osd resume` ne pourra que dire « aucun etat
        a reprendre », et l'exploitant devra tout refaire.
        """
        PipelineFaux.code = ec.IMPORT
        self.assertEqual(self.run_cli("run"), ec.IMPORT)
        self.assertEqual(self.state_run().final_code, ec.IMPORT)

    def test_l_etat_est_ecrit_apres_interruption(self):
        PipelineFaux.lever = KeyboardInterrupt
        self.assertEqual(self.run_cli("run"), ec.INTERRUPTED)
        self.assertEqual(self.state_run().final_code, ec.INTERRUPTED)


# --------------------------------------------------------------------------
# Ce que la CLI demande au pipeline
# --------------------------------------------------------------------------

class TestParametrage(CasDeTest):
    def test_check_ne_couvre_que_les_prerequis(self):
        """`check` s'arrete a l'etape 9, et le dit.

        Sans cela, un `check` en environnement de production declencherait
        un export. Le perimetre est passe explicitement plutot que deduit
        du nom de la commande : une deduction resterait invisible pour un
        lecteur, et le rapport neAllowait pas de dire « etapes 1 a 9 ».
        """
        self.run_cli("check")
        self.assertEqual(self.pipeline.kw["only"], list(range(1, 10)))
        self.assertFalse(self.pipeline.kw["dry_run"])

    def test_run_couvre_tout_le_workflow(self):
        self.run_cli("run")
        self.assertIsNone(self.pipeline.kw["only"])

    def test_dry_run_est_transmis(self):
        self.run_cli("run", "--dry-run")
        self.assertTrue(self.pipeline.kw["dry_run"])
        self.assertTrue(self.pipeline.kw["state"].dry_run)

    def test_un_dry_run_annonce_la_simulation_dans_le_rapport(self):
        """Le rapport doit dire « rien n'a ete modifie », et le prouver.

        Un rapport de simulation qui presenterait ses dix-neuf etapes
        comme des succes donnerait l'illusion d'une duplication faite.
        L'affirmation vaut par sa presence dans le document, pas par un
        detail de formatage : c'est la seule ligne que l'exploitant lit.
        """
        self.run_cli("run", "--dry-run")
        self.assertIn("SIMULATION", self.sortie)

    def test_dry_run_peut_venir_de_la_configuration(self):
        """`DRY_RUN=true` dans la configuration a le meme effet.

        Un ordonnanceur doit pouvoir arreter de produire de l'ecriture
        sans modifier la ligne de commande de sa tache.
        """
        self.run_cli("run", "--set", "DRY_RUN=true")
        self.assertTrue(self.pipeline.kw["dry_run"])

    def test_check_n_accepte_pas_de_dry_run(self):
        """`--dry-run` n'existe que sur `run`.

        Un `check` qui simulerait serait un controle qui ne controle
        rien : l'exploitant croirait avoir valide ses prerequis alors
        qu'aucune connexion n'aurait ete ouverte.
        """
        self.assertEqual(self.run_cli("check", "--dry-run"), ec.CONFIG)

    def test_resume_passe_resume(self):
        """`resume` ne se distingue de `run` que par son etat de depart.

        Il est donc verifie que le pipeline recoit bien l'etat relu, et
        non un etat neuf : un `resume` qui repartirait de zero
        recommencerait l'export, c'est-a-dire des heures.
        """
        self.poser_etat("R42")
        self.run_cli("resume", "--run-id", "R42")
        self.assertTrue(self.pipeline.kw["resume"])
        self.assertEqual(self.pipeline.kw["run_id"], "R42")
        self.assertEqual(self.pipeline.state.run_id, "R42")

    def test_force_est_propre_a_resume(self):
        """`--force` rejoue des etapes deja validees.

        Il n'a de sens que sur une reprise : sur un `run` neuf il n'y a
        rien a rejouer, et sur un `check` la notion est vide. L'accepter
        partout donnerait l'illusion d'une option disponible.
        """
        self.assertEqual(self.run_cli("run", "--force"), ec.CONFIG)
        self.assertEqual(self.run_cli("check", "--force"), ec.CONFIG)

    def test_force_ne_rejoue_qu_un_resume(self):
        self.poser_etat("R1")
        self.run_cli("resume", "--run-id", "R1", "--force")
        self.assertTrue(self.pipeline.kw["force"])

    def test_resume_sans_force_ne_rejoue_pas(self):
        """`--force` doit avoir un effet observable, sinon il est decoratif.

        La distinction reprise/rejeu est le seul interet de l'option ; un
        test qui verrait les deux formes se comporter identiquement la
        laisserait passer pour rien.
        """
        self.poser_etat("R1")
        self.run_cli("resume", "--run-id", "R1")
        self.assertFalse(self.pipeline.kw["force"])

    def test_resume_sans_echantillon_est_un_code_configuration(self):
        """Sans etat, `resume` ne peut rien faire.

        Le dire clairement vaut mieux que creer un etat vide et
        rejouer les dix-neuf etapes : l'exploitant croit a une reprise,
        et paie un export complet pour rien.
        """
        self.assertEqual(self.run_cli("resume", "--run-id", "INCONNU"), ec.CONFIG)
        self.assertFalse(PipelineFaux.instances, "le pipeline a ete construit")

    def test_resume_sans_run_id_ne_devine_pas(self):
        """`resume` refuse plutot que de choisir un run a la place de l'operateur.

        Deviner le dernier etat en date serait plus confortable, et
        dangereux : sur un serveur de saut qui heberge plusieurs bases,
        le dernier fichier depose est aussi souvent celui d'une autre
        duplication. Reprendre celle-la par megarde importerait un
        schema dans la mauvaise base -- operation irreversible.

        Le refus doit donc nommer la commande qui, elle, sait lister.
        """
        self.poser_etat("R1")
        self.assertEqual(self.run_cli("resume"), ec.CONFIG)
        self.assertIn("status", self.erreurs)
        self.assertFalse(PipelineFaux.instances, "le pipeline a ete construit")

    def test_resume_avec_run_id_dans_la_configuration(self):
        """`RUN_ID` dans la configuration est une facon de fixer la reprise.

        C'est ce qu'il faut a une tache d'ordonnanceur, dont la ligne de
        commande est fixee une fois pour toutes.
        """
        self.poser_etat("R9")
        self.run_cli("resume", "--set", "RUN_ID=R9")
        self.assertEqual(self.pipeline.state.run_id, "R9")

    def test_allow_destructive_est_transmis(self):
        self.run_cli("run", "--allow-destructive")
        self.assertTrue(self.pipeline.kw["allow_destructive"])

    def test_allow_destructive_est_refuse_par_defaut(self):
        self.run_cli("run")
        self.assertFalse(self.pipeline.kw["allow_destructive"])


# --------------------------------------------------------------------------
# Superposition des configurations
# --------------------------------------------------------------------------

class TestSuperposition(CasDeTest):
    def test_set_ecrase_le_fichier(self):
        """`--set` est la superposition de l'ordonnanceur.

        Un cron doit pouvoir corriger un fichier partage sans l'ecrire,
        donc en lecture seule.
        """
        self.run_cli("check", "--set", "SOURCE_SCHEMA=OTHER")
        self.assertEqual(self.pipeline.kw["cfg"].get("SOURCE_SCHEMA"), "OTHER")

    def test_set_est_repetable_et_normalise(self):
        self.run_cli("check", "--set", "source_schema=A", "--set", "target_schema=B")
        cfg = self.pipeline.kw["cfg"]
        self.assertEqual(cfg.get("SOURCE_SCHEMA"), "A")
        self.assertEqual(cfg.get("TARGET_SCHEMA"), "B")

    def test_la_derniere_occurrence_gagne(self):
        """Comportement deterministe, et le seul qui ne surprised pas.

        Un operateur qui repete une option s'attend a la derniere, comme
        pour n'importe quel outil en ligne de commande.
        """
        self.run_cli("check", "--set", "PARALLEL=2", "--set", "PARALLEL=8")
        self.assertEqual(str(self.pipeline.kw["cfg"].get("PARALLEL")), "8")

    def test_l_environnement_ecrase_le_fichier(self):
        os.environ["OSD_SOURCE_SCHEMA"] = "PAR_ENV"
        self.run_cli("check")
        self.assertEqual(self.pipeline.kw["cfg"].get("SOURCE_SCHEMA"), "PAR_ENV")

    def test_set_ecrase_l_environnement(self):
        """L'ordre est defauts < fichier < environnement < CLI.

        Le CLI gagne parce que c'est l'instance specifique, et
        l'environnement parce qu'il est propre a la tache. Inverser cet
        ordre rendrait `--set` inoperant des qu'un cron exporte des
        variables.
        """
        os.environ["OSD_SOURCE_SCHEMA"] = "PAR_ENV"
        self.run_cli("check", "--set", "SOURCE_SCHEMA=PAR_CLI")
        self.assertEqual(self.pipeline.kw["cfg"].get("SOURCE_SCHEMA"), "PAR_CLI")

    def test_la_configuration_de_l_environnement_seule_suffit(self):
        """Un fichier n'est pas obligatoire si l'environnement est complet.

        C'est ce qui rend l'outil deployable par variables d'environnement
        seules, sans fichier contenant une chaine de connexion.
        """
        os.environ["OSD_SOURCE_CONNECT"] = "L_SRC"
        os.environ["OSD_TARGET_CONNECT"] = "L_TGT"
        os.environ["OSD_SOURCE_SCHEMA"] = "HR"
        os.environ["OSD_TARGET_SCHEMA"] = "UAT"
        os.environ["OSD_SOURCE_DIRECTORY"] = "DP_SRC"
        os.environ["OSD_TARGET_DIRECTORY"] = "DP_TGT"
        os.environ["OSD_LOG_DIR"] = str(self.base / "logs")
        os.environ["OSD_WORK_DIR"] = str(self.base / "work")
        os.environ["OSD_REPORT_DIR"] = str(self.base / "reports")
        self.assertEqual(self.run_cli("check"), ec.SUCCESS)

    def test_une_cle_inconnue_est_refusee(self):
        """Une faute de frappe sur une cle doit etre bruyante.

        L'ignorer rendrait l'outil silencieusement inoperant sur ce
        parametre, et l'exploitant verrait un rapport de succes.
        """
        self.assertEqual(self.run_cli("check", "--set", "PARALEL=4"), ec.CONFIG)

    def test_un_fichier_absent_est_refuse(self):
        self.assertEqual(
            self.main("-c", str(self.racine / "absent.conf"), "check"), ec.CONFIG
        )

    def test_une_cle_secrete_n_atteint_pas_la_sortie(self):
        """Un secret demande explicitement ne doit pas etre rejoue.

        Il est ici pose par `--set` plutot que par variable
        d'environnement, parce que la ligne de commande est le canal que
        l'exploitant a coutume d'ecrire dans un ticket.
        """
        self.run_cli("check", "--set", "SOURCE_PASSWORD=TRES_SECRET")
        self.assertNotIn("TRES_SECRET", self.sortie)
        self.assertNotIn("TRES_SECRET", self.erreurs)


# --------------------------------------------------------------------------
# Les sous-commandes hors pipeline
# --------------------------------------------------------------------------

class TestSousCommandes(CasDeTest):
    def test_version(self):
        self.assertEqual(self.main("version"), ec.SUCCESS)
        self.assertIn("osd", self.sortie)

    def test_config_liste_le_schema(self):
        self.assertEqual(self.main("config"), ec.SUCCESS)
        self.assertIn("SOURCE_SCHEMA", self.sortie)
        self.assertIn("TARGET_SCHEMA", self.sortie)

    def test_config_detaille_une_cle(self):
        self.assertEqual(self.main("config", "parallel"), ec.SUCCESS)
        self.assertIn("PARALLEL", self.sortie)

    def test_config_sur_une_cle_inconnue_est_un_code_configuration(self):
        self.assertEqual(self.main("config", "CLE_INEXISTANTE"), ec.CONFIG)

    def test_config_ne_mentionne_pas_de_valeur_de_secret(self):
        """Le schema peut etre liste : il ne contient aucun secret.

        Il doit tout de meme signaler quelles cles le sont, sans jamais
        afficher de valeur.
        """
        self.main("config")
        self.assertIn("secret", self.sortie)
        self.assertNotIn("@//", self.sortie)

    def test_status_sans_rien_a_montrer(self):
        self.assertEqual(self.run_cli("status"), ec.SUCCESS)

    def test_status_avec_aucun_repertoire_de_travail(self):
        """Un serveur de saut neuf ne doit pas renvoyer une erreur."""
        self.assertEqual(self.run_cli("status"), ec.SUCCESS)
        self.assertIn("aucun run", self.sortie)

    def test_clean_refuse_sans_autorisation(self):
        """`clean` supprime l'etat de reprise : c'est destructif.

        Le refus doit porter le code 8, et non 1 : un exploitant qui
        automate la suppression par erreur voit un code qui signifie
        « garde-fou de securite », ce qui le ramene a reflechir.
        """
        (self.base / "work").mkdir(parents=True, exist_ok=True)
        (self.base / "work" / "state-R1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_cli("clean"), ec.SECURITY)
        self.assertTrue((self.base / "work" / "state-R1.json").exists())

    def test_clean_avec_autorisation(self):
        (self.base / "work").mkdir(parents=True, exist_ok=True)
        cible = self.base / "work" / "state-R1.json"
        cible.write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_cli("clean", "--allow-destructive"), ec.SUCCESS)
        self.assertFalse(cible.exists())

    def test_clean_rien_a_nettoyer(self):
        self.assertEqual(self.run_cli("clean", "--allow-destructive"), ec.SUCCESS)


# --------------------------------------------------------------------------
# Rapport et flux
# --------------------------------------------------------------------------

class TestRapport(CasDeTest):
    def test_le_rapport_json_est_bien_forme(self):
        """`--json` doit produire du JSON, pas un rapport_txt deguise.

        C'est le mode qu'un orchestrateur consomme ; un JSON invalide le
        fait echouer en silence cote consommateur.
        """
        self.run_cli("run", "--json")
        document = json.loads(self.sortie[self.sortie.index("{"):
                                          self.sortie.rindex("}") + 1])
        self.assertIn("steps", document)

    def test_le_rapport_texte_est_sur_stdout(self):
        self.run_cli("run")
        self.assertIn("etape", self.sortie.lower())

    def test_le_journal_atterrit_sur_stderr(self):
        """La separation des flux est une garantie, pas une convenance.

        Un `osd run > rapport.txt` doit produire un rapport exploitable.
        Melange au journal, il faudrait deviner quelle partie
        l'ordonnanceur doit relire apres un echec.

        L'avertissement emis ici est celui du fichier de configuration
        lisible par d'autres : il est produit par le **journal**, donc
        sa presence sur stderr et son absence sur stdout prouvent le
        routage. Chercher plutot une banniere d'etape testerait le
        pipeline, pas la CLI.
        """
        self.config.chmod(0o644)  # declenche l'avertissement
        self.run_cli("run")
        self.assertIn("lisible par d'autres", self.erreurs,
                      "le journal n'est pas sur stderr")
        self.assertNotIn("lisible par d'autres", self.sortie,
                         "le journal est sur stdout")

    def test_le_rapport_ne_depend_pas_du_journal(self):
        """Meme principe, verifie du cote du rapport.

        Le rapport doit se suffire a lui-meme : c'est lui qu'on archive
        et qu'on transmet, pas le journal.
        """
        self.config.chmod(0o644)
        self.run_cli("run")
        self.assertIn("VERDICT", self.sortie)
        self.assertIn("1. charger-configuration", self.sortie)

    def test_le_rapport_complet_tient_dans_stdout(self):
        """Ce qui doit rester sur stdout,Precisement : le rapport.

        Un `osd run --json | jq` ne doit pas avoir a filtrer le journal
        pour trouver son document.
        """
        self.run_cli("run", "--json")
        document = json.loads(self.sortie[self.sortie.index("{"):
                                          self.sortie.rindex("}") + 1])
        self.assertEqual(len(document["steps"]), 19)
        for etape in document["steps"]:
            self.assertIn("name", etape)

    def test_le_rapport_est_ecrit_sur_disque(self):
        self.run_cli("run")
        rapports = list((self.base / "reports").glob("report-*"))
        self.assertTrue(rapports, "aucun rapport ecrit")

    def test_le_format_json_ne_s_ecrit_que_si_demande(self):
        """Ecrire deux fois le meme rapport par defaut serait du gaspillage.

        Le rapport texte est destine a l'ecran, le JSON a la machine.
        Un serveur de saut qui en produit deux par run remplit son disque
        de rapports que personne ne lira.
        """
        self.run_cli("run")
        suffixes = {p.suffix for p in (self.base / "reports").glob("report-*")}
        self.assertEqual(suffixes, {".txt"})

    def test_le_format_both_ecrit_les_deux(self):
        self.run_cli("run", "--set", "REPORT_FORMAT=BOTH")
        suffixes = {p.suffix for p in (self.base / "reports").glob("report-*")}
        self.assertEqual(suffixes, {".txt", ".json"})

    def test_le_rapport_est_lisible_par_son_seul_proprietaire(self):
        """Un rapport inventorie les objets d'un schema.

        C'est une information sur le systeme d'information, pas une donnee
        publique : `0644` la livrerait a tout le monde du serveur de saut.
        """
        self.run_cli("run")
        for path in (self.base / "reports").glob("report-*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_le_rapport_ne_fuit_aucun_secret(self):
        os.environ["OSD_SOURCE_PASSWORD"] = "TRES_SECRET"
        self.run_cli("run")
        for path in list((self.base / "reports").glob("report-*")):
            self.assertNotIn("TRES_SECRET", path.read_text(encoding="utf-8"),
                             f"secret present dans {path}")
        self.assertNotIn("TRES_SECRET", self.sortie)

    def test_un_rapport_non_ecrit_ne_change_pas_le_code(self):
        """La duplication est deja faite quand le rapport echoue.

        Rendre un code d'echec ferait relancer un traitement reussi, et
        l'exploitant decouvrirait un schema duplique deux fois. Le
        code doit rester celui de l'operation, et l'incident etre
        signale.
        """
        self.base.mkdir(parents=True, exist_ok=True)
        (self.base / "reports").write_text("pas un repertoire", encoding="utf-8")
        code = self.run_cli("run")
        self.assertEqual(code, ec.SUCCESS)

    def test_quiet_supprime_le_journal_sur_stderr(self):
        """`--quiet` concerne le journal, jamais le resultat.

        Silencer aussi le rapport rendrait l'option inutile : c'est le
        journal qui est bruyant, et le rapport qui est la reponse. Sous
        cron, c'est le rapport qu'on archive.
        """
        self.config.chmod(0o644)  # declenche l'avertissement
        self.run_cli("run", "--quiet")
        self.assertNotIn("lisible par d'autres", self.erreurs)
        self.assertIn("VERDICT", self.sortie)


# --------------------------------------------------------------------------
# Verrou et concurrence, vus par la CLI
# --------------------------------------------------------------------------

class TestVerrou(CasDeTest):
    """Le verrou vu de la CLI — donc **de deux processus**.

    Impossible de tenir le verrou dans le processus de test : un verrou
    `fcntl` appartient au processus, si bien qu'une seconde prise depuis
    le meme processus reussit toujours. Un test qui pretendrait le
    contraire passerait sans rien prouver — c'est ce qui est arrive tant
    que la prise etait faite ici meme. Le detenteur est donc un vrai
    fils, et la liberation passe par la fermeture de son entree
    standard.
    """

    #: Enfant qui prend le verrou et attend sur stdin.
    _TENIR = (
        "import sys\n"
        "sys.path.insert(0, {src!r})\n"
        "from pathlib import Path\n"
        "from osd.lock import Lock\n"
        "with Lock(Path(sys.argv[1]), description=sys.argv[2]):\n"
        "    print('PRIS', flush=True)\n"
        "    sys.stdin.read()\n"
    )

    def tenir_le_verrou(self, description: str = "premier"):
        """Demarre un fils qui tient le verrou de cette configuration.

        Le chemin n'est pas devinable a la main : il derive de la
        configuration. Le recalculer ici, a partir du **meme** fichier
        que la CLI va charger, est la seule facon d'atteindre reellement
        le verrou qu'elle utilise.
        """
        import subprocess
        import sys as _sys

        from osd import config as config_mod
        from osd.lock import lock_key

        cfg = config_mod.load(self.config)
        chemin = self.base / "locks" / f"{lock_key(cfg)}.lock"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        code = self._TENIR.format(src=str(SRC_DIR))
        proc = subprocess.Popen(
            [_sys.executable, "-u", "-c", code, str(chemin), description],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._relacher, proc)
        entete = proc.stdout.readline().decode()
        self.assertTrue(entete.startswith("PRIS"),
                        f"le fils n'a pas pris le verrou : {entete!r}")
        return proc

    @staticmethod
    def _relacher(proc: Any) -> None:
        """Laisse le fils sortir proprement, puis ferme ses trois tubes.

        `Popen` n'atteint pas les attributs de `file` du GC : sans cette
        fermeture, chaque test de cette classe laisse un descripteur
        ouvert, et l'avertissement `ResourceWarning` noie la sortie
        reelle de la suite.
        """
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=30)
        except Exception:  # pragma: no cover - meilleur effort
            proc.kill()
            proc.wait(timeout=30)
        finally:
            for tube in (proc.stdout, proc.stderr):
                if tube is not None and not tube.closed:
                    tube.close()

    def test_un_verrou_deja_tenu_refuse_le_run(self):
        """C'est la raison d'etre du verrou, vue de la CLI.

        Deux `osd` sur le meme couple de schemas s'ecraseraient mutuellement
        sans qu'aucun des deux ne signale rien : ni erreur, ni avertis-
        sement, deux rapports de succes, et un schema ecrase que rien ne
        signale.
        """
        self.tenir_le_verrou()
        self.assertEqual(self.run_cli("run"), ec.PREREQ)
        # Le pipeline est **construit** avant la prise du verrou -- sa
        # construction ne touche ni la base ni le reseau. Ce qui doit
        # rester faux, c'est l'execution : aucun controle ne doit avoir
        # ete fait, et surtout aucun `expdp` n'a pu partir.
        self.assertFalse(self.pipeline.execute_appele, "le run a demarre")

    def test_le_verrou_indique_qui_le_detient(self):
        """« deja pris » sans nom n'est exploitable par personne.

        L'exploitant doit pouvoir repondre a la seule question qui
        compte : est-ce que j'attends un run legitime, ou est-ce que
        j'ouvre une session restee en plan ?
        """
        self.tenir_le_verrou("premier")
        code = self.run_cli("run")
        self.assertEqual(code, ec.PREREQ)
        self.assertIn("premier", self.erreurs)

    def test_le_verrou_est_relache_apres_interruption(self):
        """Un verrou non relaye bloque la reprise — le cas du signal.

        SIGINT leve une exception ; si le verrou restait pris, l'execution
        suivante — celle de la reprise — se refuserait elle-meme l'entree.
        L'exploitant qui relance apres un arret se verrait donc interdire
        de finir ce qu'il a commence, avec un message parlant d'une
        duplication identique deja en cours : alors que lui-meme l'a
        interrompue.
        """
        PipelineFaux.lever = KeyboardInterrupt
        self.assertEqual(self.run_cli("run"), ec.INTERRUPTED)
        PipelineFaux.lever = None
        self.assertEqual(self.run_cli("run"), ec.SUCCESS,
                         "le verrou n'a pas ete libere apres l'interruption")

    def test_le_verrou_est_relache_apres_echec(self):
        PipelineFaux.code = ec.EXPORT
        self.assertEqual(self.run_cli("run"), ec.EXPORT)
        self.assertEqual(self.run_cli("run"), ec.EXPORT,
                         "le verrou n'a pas ete libere apres l'echec")

    def test_un_lock_dir_inutilisable_est_un_code_prerequis(self):
        """Un `LOCK_DIR` qui n'est pas un repertoire n'est pas un bogue.

        C'est une erreur d'environnement — point de montage en lecture
        seule, quota epuise, chemin errone — donc le code 2, qui oriente
        vers la configuration. Le code 1 (« configuration invalide »)
        Panicait l'exploitant vers une faute de frappe alors que le
        probleme etait le systeme de fichiers.
        """
        self.base.mkdir(parents=True, exist_ok=True)
        (self.base / "locks").write_text("fichier, pas repertoire", encoding="utf-8")
        self.assertEqual(self.run_cli("run"), ec.PREREQ)
        self.assertIn("verrou", self.erreurs.lower())


# --------------------------------------------------------------------------
# Les signaux
# --------------------------------------------------------------------------

class TestSignaux(unittest.TestCase):
    """SIGINT et SIGTERM, vus par le gestionnaire lui-meme.

    Le signal est **rejoue** plutot qu'envoye depuis l'exterieur :
    `os.kill(os.getpid(), ...)` tuerait le test au moment ou il
    verifie justement que rien ne meurt. Rejouer le gestionnaire lit la
    meme fonction, avec le meme etat de compteur, ce qui est toute la
    logique en jeu.
    """

    def installer(self):
        from osd.cli import _install_signal_handlers
        from osd.logging_setup import get_logger

        _install_signal_handlers(get_logger())
        return signal.getsignal(signal.SIGINT)

    def setUp(self) -> None:
        # Les gestionnaires de signaux ne se registrent qu'en fil
        # principal et sont herites par les processus enfants : les
        # restaurer est une politesse envers le lanceur de tests.
        self._originaux = {s: signal.getsignal(s)
                           for s in (signal.SIGINT, signal.SIGTERM)}
        self.addCleanup(self._restaurer)

    def _restaurer(self) -> None:
        for sig, ancien in self._originaux.items():
            try:
                signal.signal(sig, ancien)
            except (ValueError, OSError):  # pragma: no cover
                pass

    def test_un_premier_signal_demande_un_arret_propre(self):
        """SIGINT ne doit pas `sys.exit` sur-le-champ.

        Un `exit` brutal laisserait un verrou bloque et aucun etat : la
        reprise, qui est precisement la reponse a une interruption, serait
        alors impossible. Le gestionnaire leve donc une exception, que les
        gestionnaires de contexte savent traverser.
        """
        handler = self.installer()
        self.assertTrue(callable(handler))
        with self.assertRaises(Interrupted):
            handler(signal.SIGINT, None)

    def test_sigterm_est_traite_comme_sigint(self):
        """`kill` par l'ordonnanceur doit arreter proprement aussi.

        C'est le signal que recoit un run tue par unordonnanceur qui
        bascule. Le traiter differemment de SIGINT rendrait l'arret
        propre indisponible en production, la ou il sert le plus.
        """
        handler = self.installer()
        with self.assertRaises(Interrupted):
            handler(signal.SIGTERM, None)

    def test_l_interruption_nomme_le_signal_recu(self):
        """Le message doit nommer le signal, pas dire « interruption ».

        Un exploitant qui a envoye SIGTERM pour etre gentil, et qui lit
        « SIGINT recu », ne saura pas que son TERM a ete traite comme
        un Ctrl-C : il enverra un second signal pour rien.

        Un seul signal par test, sur une installation neuve a chaque
        fois : deux signaux sur le **meme** gestionnaireatteignent le
        branche du second signal, qui appelle `os._exit` et tuerait le
        lanceur de tests.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig.name):
                handler = self.installer()
                with self.assertRaises(Interrupted) as ctx:
                    handler(sig, None)
                self.assertIn(sig.name, str(ctx.exception))

    def test_un_second_signal_donne_la_main_a_l_os(self):
        """Un exploitant qui a attendu doit pouvoir arreter definitivement.

        Le second signal appelle `os._exit`, qui ne passe par aucun
        gestionnaire de contexte : ni verrou libere, ni etat enregistre.
        C'est deliberé — c'est le seul moyen de sortir d'un etat bloque —
        et c'est pourquoi il ne faut pas l'employer au premier signal.
        """
        sorties: List[int] = []
        reel = os._exit
        os._exit = sorties.append  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(os, "_exit", reel))

        handler = self.installer()
        with self.assertRaises(Interrupted):
            handler(signal.SIGINT, None)
        self.assertEqual(sorties, [], "sortie prematuree au premier signal")

        handler(signal.SIGINT, None)
        self.assertEqual(sorties, [ec.INTERRUPTED])

    def test_le_compteur_est_propre_a_l_installation(self):
        """Deux executions dans le meme processe ne se transmettre pas le compte.

        Le compteur vit dans la fermeture installee par un appel. S'il
        etait global, le second signal d'une execution aurait herite du
        premier d'une autre — et `os._exit` tuerait le processus avant
        qu'il ait enregistre quoi que ce soit.
        """
        self.installer()
        handler = signal.getsignal(signal.SIGINT)
        with self.assertRaises(Interrupted):
            handler(signal.SIGINT, None)

        sorties: List[int] = []
        reel = os._exit
        os._exit = sorties.append  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(os, "_exit", reel))

        nouveau = self.installer()
        with self.assertRaises(Interrupted):
            nouveau(signal.SIGINT, None)
        self.assertEqual(sorties, [])


# --------------------------------------------------------------------------
# Analyse des arguments
# --------------------------------------------------------------------------

class TestSetOverride(unittest.TestCase):
    def test_la_cle_est_normalisee(self):
        """`--set parallel=4` et `--set PARALLEL=4` sont equivalents.

        Une commodite, mais une commodite qui evite un echec stupide
        sous contrainte de temps, et une incoherence avec les variables
        d'environnement, qui sont deja en majuscules.
        """
        from osd.cli import _parse_args

        for forme in ("parallel=4", "PARALLEL=4", "PaRaLlEl=4"):
            with self.subTest(forme=forme):
                args = _parse_args(["run", "--set", forme])
                self.assertEqual(args.overrides, {"PARALLEL": "4"})

    def test_les_espaces_autour_du_egal_sont_toleres(self):
        """`--set PARALLEL = 4` doit fonctionner comme `--set PARALLEL=4`.

        C'est la forme qu'un operateur ecrit spontanement, et une valeur
        bordée d'espaces ne l'est jamais volontairement. Le separateur
        etant un `=`, le shell ne peut pas dedouaner : c'est donc a
        l'outil de le faire, sous peine d'echouer sur la maniere dont la
        ligne a ete quotee.
        """
        from osd.cli import _parse_args

        args = _parse_args(["run", "--set", " PARALLEL = 4 "])
        self.assertEqual(args.overrides, {"PARALLEL": "4"})

    def test_la_valeur_conserve_ses_espaces(self):
        """Seule la cle est normalisee.

        Une valeur peut legitimement contenir des espaces : un chemin de
        repertoire, un nom de fichier. Les retrancher produirait une
        configuration silencieusement fausse.
        """
        from osd.cli import _parse_args

        args = _parse_args(["run", "--set", "WORK_DIR=/donnees/mes fichiers"])
        self.assertEqual(args.overrides["WORK_DIR"], "/donnees/mes fichiers")
        # Les espaces **internes** sont significatifs, ceux des bords ne
        # le sont pas : un chemin ne commence ni ne finit par une espace.
        bords = _parse_args(["run", "--set", "WORK_DIR= /donnees/x "])
        self.assertEqual(bords.overrides["WORK_DIR"], "/donnees/x")

    def test_une_valeur_vide_est_acceptee(self):
        """Vider une cle est un geste legitime : desactiver une valeur.

        `LOCK_DIR=` retombe sur `WORK_DIR`, `SOURCE_HOST=` rend le cote
        local. Refuser la chaine vide interdirait ces usages.
        """
        from osd.cli import _parse_args

        args = _parse_args(["run", "--set", "SOURCE_HOST="])
        self.assertEqual(args.overrides["SOURCE_HOST"], "")

    def test_une_seule_esperance_egale(self):
        """`--set CLE=a=b` garde tout ce qui suit la premiere.

        Partitionner sur le premier `=` est la seule lecture qui
        n'exclut aucune valeur utilisable.
        """
        from osd.cli import _parse_args

        args = _parse_args(["run", "--set", "EXTRA=a=b"])
        self.assertEqual(args.overrides["EXTRA"], "a=b")

    def test_une_cle_non_alphanumerique_est_refusee(self):
        """Une cle avec un point ou un tiret n'est pas une cle.

        L'ignorer ne serait pas plus poli : la validation du schema
        dirait « cle inconnue », et l'exploitant chercherait au lieu de
        comprendre qu'il a mal orthographie le nom.
        """
        from osd.cli import _parse_args

        for cle in ("A-B=1", "A.B=1", "A B=1", "=1"):
            with self.subTest(cle=cle):
                with self.assertRaises(SystemExit) as ctx:
                    with contextlib.redirect_stderr(io.StringIO()):
                        _parse_args(["run", "--set", cle])
                self.assertNotEqual(ctx.exception.code, 0)

    def test_les_cles_sont_independantes_de_l_ordre(self):
        from osd.cli import _parse_args

        un = _parse_args(["run", "--set", "A=1", "--set", "B=2"]).overrides
        autre = _parse_args(["run", "--set", "B=2", "--set", "A=1"]).overrides
        self.assertEqual(un, autre)


if __name__ == "__main__":
    unittest.main()
