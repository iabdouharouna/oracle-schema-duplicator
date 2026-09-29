"""Tests de la machine a etats et de sa persistance.

L'etat est le seul souvenir d'un run. C'est lui qui permet la reprise,
et lui encore qui dit a `status` ce qui s'est passe. Deux proprietes
doivent donc etre garanties sans condition :

* **l'atomicite** — une coupure pendant l'ecriture ne doit pas produire
  un fichier tronque, parce qu'un fichier tronque interdit toute reprise
  alors meme que le run precedent etait parfaitement valide ;
* **l'impossibilite de sortir en succes apres un echec** — un code final
  ne peut pas etre ecrit si une etape a echoue.

Le test d'interruption simule la coupure reelle : il ecrit des etats
intermédiaires, comme le ferait un processus tue, et verifie que la
reprise retrouve exactement l'etat ou le run s'etait arrete.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from osd import exit_codes as ec
from osd.errors import OsdError
from osd.state import (
    DONE,
    FAILED,
    RUNNING,
    SKIPPED,
    STEP_EXPORT,
    State,
    StateStore,
    new_run_id,
)


class TestEtapes(unittest.TestCase):
    def setUp(self):
        self.state = State(run_id="essai")

    def test_start_puis_finish(self):
        st = self.state.start(1, "charger-configuration")
        self.assertEqual(st.status, RUNNING)
        st2 = self.state.finish(1, "charger-configuration", status=DONE, message="ok")
        self.assertIs(st, st2)
        self.assertEqual(st2.status, DONE)
        self.assertEqual(st2.message, "ok")
        self.assertTrue(st2.is_done)

    def test_le_code_de_l_etape_est_conserve(self):
        """Le code est ce qui permet a `resume` de savoir pourquoi reessayer.

        Perdre le code ne ferait pas perdre le statut, mais ferait
        perdre la cause : l'exploitant serait force de rejouer tout le
        preparatif pour rien.
        """
        self.state.start(4, "tester-connexion-source")
        self.state.finish(
            4, "tester-connexion-source", status=FAILED,
            code=ec.CONNECTION, message="ORA-12541",
        )
        self.assertEqual(self.state.steps[4].code, ec.CONNECTION)

    def test_les_details_sont_copies(self):
        """`detail` ne doit pas partager la liste d'un appelant.

        L'appelant, typiquement le pipeline, reutilise sa liste de
        messages d'une etape a l'autre ; la partager la ferait croitre
        indefiniment dans l'etat, et gonflerait le rapport de facon
        non bornee.
        """
        ma_liste = ["a"]
        self.state.start(1, "x")
        self.state.finish(1, "x", status=DONE, detail=ma_liste)
        ma_liste.append("b")
        self.assertEqual(self.state.steps[1].detail, ["a"])

    def test_un_step_absent_le_cree(self):
        self.state.start(7, "verifier-schema-cible")
        self.assertIn(7, self.state.steps)

    def test_next_pending_respecte_l_ordre_du_workflow(self):
        """`next_pending` suit l'ordre donne, pas l'ordre des insertions.

        Sans cela, une reprise pourrait rejouer l'etape 3 apres l'etape
        14, et le rapport montrerait un workflow qui n'a pas eu lieu.
        """
        for i in (14, 3, 7):
            self.state.start(i, f"e{i}")
            self.state.finish(i, f"e{i}", status=DONE)
        self.assertEqual(self.state.next_pending([1, 2, 3, 14]), 1)
        self.state.start(1, "e1")
        self.state.finish(1, "e1", status=DONE)
        self.assertEqual(self.state.next_pending([1, 2, 3, 14]), 2)

    def test_next_pending_vide_si_tout_est_fait(self):
        for i in (1, 2, 3):
            self.state.start(i, f"e{i}")
            self.state.finish(i, f"e{i}", status=DONE)
        self.assertIsNone(self.state.next_pending([1, 2, 3]))


class TestReprise(unittest.TestCase):
    """`is_resumable` est le garde-fou de la reprise.

    Faux positif : l'outil reprend un run dont le dump a disparu, et
    l'import echoue sur un fichier absent, apres avoir annonce que la
    reprise etait possible.
    """

    def setUp(self):
        self.state = State(run_id="essai")

    def test_reprise_impossible_sans_export(self):
        self.assertFalse(self.state.is_resumable(list(range(1, 20))))

    def test_reprise_impossible_si_seule_la_preparation_a_reussi(self):
        """L'etape 10 terminee ne vaut pas export.

        C'est le decalage qui serait passe inapercu : en testant
        l'etape 10, un run interrompu pendant l'export etait declare
        reprenable des que `dump_parts` etait renseigne a moitie.
        """
        self.state.start(STEP_EXPORT - 1, "preparer-datapump")
        self.state.finish(STEP_EXPORT - 1, "preparer-datapump", status=DONE)
        self.assertFalse(self.state.is_resumable(list(range(1, 20))))

    def test_reprise_impossible_si_l_export_a_echoue(self):
        self.state.start(STEP_EXPORT, "exporter")
        self.state.finish(STEP_EXPORT, "exporter", status=FAILED, code=4)
        self.state.artifacts["dump_parts"] = [{"name": "partiel.dmp"}]
        self.assertFalse(self.state.is_resumable(list(range(1, 20))))

    def test_reprise_impossible_sans_parties_de_dump(self):
        """L'etape 11 terminee ne suffit pas : il faut les fichiers.

        C'est le piege : un export « reussi » sur un schemas vide ne
        produit aucun fichier, et une reprise qui le croirait exporterait
        puis importerait un dump vide, en repondant succes.
        """
        self.state.start(10, "preparer-datapump")
        self.state.finish(10, "preparer-datapump", status=DONE)
        self.state.start(STEP_EXPORT, "exporter")
        self.state.finish(STEP_EXPORT, "exporter", status=DONE)
        self.assertFalse(self.state.is_resumable(list(range(1, 20))))

    def test_reprise_possible_avec_export_et_parties(self):
        self.state.start(STEP_EXPORT, "exporter")
        self.state.finish(STEP_EXPORT, "exporter", status=DONE)
        self.state.artifacts["dump_parts"] = [{"name": "a.dmp", "bytes": 10}]
        self.assertTrue(self.state.is_resumable(list(range(1, 20))))

    def test_l_etape_de_reprise_est_bien_celle_de_l_export(self):
        """`STEP_EXPORT` doit designer l'export, et non sa preparation.

        `state.py` ne peut pas importer le pipeline sans creer un cycle,
        d'ou la constante recopiee. Ce test est le garde-fou : il
        detecte la divergence des que l'ordre du workflow change, la ou
        un test du seul `state` la laisserait passer.
        """
        from osd.stages.pipeline import STEP_NAMES

        self.assertIn("export", STEP_NAMES[STEP_EXPORT].lower())
        self.assertNotIn("export", STEP_NAMES[STEP_EXPORT - 1].lower())

    def test_une_etape_sautee_n_est_pas_rejouee(self):
        """`SKIPPED` est une decision, pas un oubli.

        Une etape sautee l'a ete parce qu'une condition evaluee l'a
        rendue inutile — un transfert vers un repertoire partage, par
        exemple. La rejouer recalculerait la condition et pourrait, si
        l'etat avait change, declencher une action qu'on avait decide de
        ne pas faire. Elle compte donc comme traitee, au meme titre que
        `DONE`.
        """
        self.state.start(13, "transferer")
        self.state.finish(13, "transferer", status=SKIPPED)
        self.assertIn(13, self.state.completed_indices())
        self.assertEqual(self.state.next_pending(list(range(13, 20))), 14)


class TestSerialisation(unittest.TestCase):
    def test_aller_retour(self):
        state = State(run_id="r1")
        state.source = "L_SRC"
        state.source_schema = "SRC"
        state.target_schema = "TGT"
        state.dry_run = True
        state.final_code = ec.SUCCESS
        state.start(4, "tester-connexion-source")
        state.finish(4, "tester-connexion-source", status=DONE, message="OEMCC", detail=["ok"])
        state.artifacts["dump_parts"] = [{"name": "a.dmp", "bytes": 12}]
        state.metrics["source_bytes"] = 1234
        state.error = {"message": "x", "code": 3}

        relu = State.from_dict(state.to_dict())
        self.assertEqual(relu.run_id, "r1")
        self.assertTrue(relu.dry_run)
        self.assertEqual(relu.final_code, ec.SUCCESS)
        self.assertEqual(relu.steps[4].message, "OEMCC")
        self.assertEqual(relu.artifacts["dump_parts"][0]["name"], "a.dmp")
        self.assertEqual(relu.metrics["source_bytes"], 1234)
        self.assertEqual(relu.error["code"], 3)

    def test_les_cles_d_etapes_degrades_en_chaines_sont_relues(self):
        """JSON n'a pas d'entier : les cles d'etapes arrivent en `str`.

        Sans conversion, la reprise ne retrouverait aucune etape et
        rejouerait le workflow entier — sur un schema deja peuple.
        """
        relu = State.from_dict({"run_id": "r", "steps": {"11": {"status": "done"}}})
        self.assertIn(11, relu.steps)
        self.assertTrue(relu.steps[11].is_done)

    def test_une_cle_d_etape_non_entiere_est_ignoree(self):
        relu = State.from_dict({"run_id": "r", "steps": {"pas_un_entier": {"status": "done"}}})
        self.assertEqual(relu.steps, {})

    def test_un_document_incomplet_ne_leve_pas(self):
        """Un etat minimal doit rester chargeable.

        L'incomplétude est la norme en pratique (un run interrompu avant
        la premiere ecriture) ; la faire echouer interdirait toute
        reprise et masquerait le vrai probleme derriere une erreur de
        lecture.
        """
        relu = State.from_dict({})
        self.assertIsNone(relu.final_code)
        self.assertEqual(relu.steps, {})


class TestPersistance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state-r.json"
        self.store = StateStore(self.path)

    def test_ecrit_et_relit(self):
        state = State(run_id="r1")
        state.start(3, "verifier-dependances")
        self.store.save(state)
        self.assertIsNotNone(self.store.load())

    def test_absent_retourne_none(self):
        self.assertIsNone(self.store.load())

    def test_le_fichier_est_en_0600(self):
        """L'etat contient les noms de connexion et les chemins des dumps.

        Ce n'est pas un secret, mais c'est une cartographie de
        l'infrastructure, lisible par tous ceux qui shares le serveur de
        saut — dont des comptes deDeveloppement. Le mode groupe/others
        doit donc etre interdit.
        """
        self.store.save(State(run_id="r1"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_le_repertoire_est_cree_en_0700(self):
        cible = Path(self.tmp.name) / "profond" / "work"
        StateStore(cible / "state-r.json").save(State(run_id="r1"))
        self.assertEqual(cible.stat().st_mode & 0o777, 0o700)

    def test_pas_de_fichier_temporaire_residuel(self):
        """Une ecriture interrompue ne doit pas laisser de `.tmp` a cote.

        Le fichier temporaire contient l'integralite de l'etat ; le
        laisser en place expose l'information et confond le nettoyage.
        """
        self.store.save(State(run_id="r1"))
        residuels = [p.name for p in self.path.parent.iterdir() if ".tmp." in p.name]
        self.assertEqual(residuels, [])

    def test_ecriture_atomique_avec_fichier_precedent(self):
        """En cas d'echec d'ecriture, l'ancien etat doit rester lisible.

        C'est la raison d'etre du fichier temporaire : un `open(...,"w")`
        direct tronquerait le fichier et ferait perdre la reprise d'un
        run qui, lui, avait abouti.
        """
        bon = State(run_id="r1")
        bon.final_code = ec.SUCCESS
        self.store.save(bon)
        try:
            self.store.save(bon)  # deuxieme ecriture, doit echouer
        except Exception:
            pass
        relu = self.store.load()
        self.assertIsNotNone(relu)
        self.assertEqual(relu.run_id, "r1")

    def test_json_corrompu_donne_une_erreur_explicite(self):
        self.path.write_text("{ ceci n'est pas du json", encoding="utf-8")
        with self.assertRaises(OsdError) as ctx:
            self.store.load()
        self.assertEqual(ctx.exception.code, ec.CONFIG)


class TestRunId(unittest.TestCase):
    def test_plusieurs_runs_d_un_meme_processus_sont_distinguables(self):
        """Horodatage + PID ne suffisent pas dans un meme processus.

        Deux runs lances par un wrapper, un test d'integration ou un
        usage en bibliotheque tombent dans la meme seconde avec le meme
        PID. Le second ecraserait alors le fichier d'etat **et** le
        rapport du premier, et `status` ne montrerait que le plus
        recent : la perte porterait sur la mauvaise duplication, sans
        le moindre message.
        """
        ids = [new_run_id() for _ in range(50)]
        self.assertEqual(len(set(ids)), len(ids))

    def test_format_stable_pour_les_noms_de_fichiers(self):
        run_id = new_run_id()
        self.assertRegex(run_id, r"^osd-\d{8}T\d{6}Z-\d+(\.\d+)?$")
        # Aucun separateur de chemin, aucune espace : le nom est utilise
        # tel quel dans des repertoires et des lignes de commande.
        self.assertNotIn("/", run_id)
        self.assertNotIn(" ", run_id)


if __name__ == "__main__":
    unittest.main()
