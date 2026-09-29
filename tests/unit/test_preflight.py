"""Tests des controles prealables, sur adaptateur simule.

Ce module couvre les quatorze scenarios de `AGENTS.md` qui sont
decidables **sans base ni reseau**. C'est la partie du contrat qui
l'outillage industriel doit garantir : un controle qui ne peut pas etre
verifie sans allumer une base n'est pas un controle, c'est une croyance.

Chaque classe porte le numero du scenario qu'elle couvre, pour que la
correspondance avec `AGENTS.md` soit lisible sans les deux fichiers
cote a cote. Le scenario 14 (reprise) et le scenario 11 (erreur
Oracle) debordent sur `test_pipeline.py` et `test_datapump.py`.
"""

from __future__ import annotations

import unittest

import support
from support import FakeAdapter, FakeRunner

from osd import exit_codes as ec
from osd.checks import preflight
from osd.checks.preflight import FAIL, OK, SKIP, WARN
from osd.errors import ConnectionError_, OsdError, PrereqError

MO = 1024 * 1024
GO = 1024 * MO


class TestScenario01ConnexionSourceIndisponible(unittest.TestCase):
    """« connexion source indisponible »"""

    def test_echec_avec_le_code_connexion(self):
        adapter = FakeAdapter(
            check_connection=ConnectionError_(
                "ORA-12541: no listener", hint="Verifier le listener."
            )
        )
        r = preflight.check_connection("source", adapter)
        self.assertEqual(r.status, FAIL)
        self.assertEqual(r.code, ec.CONNECTION)
        self.assertIn("ORA-12541", r.message)

    def test_le_remede_de_l_erreur_est_reconduit(self):
        """Un message sans remede oblige a retablir le diagnostic a la main.

        Le remede est produit par la couche qui **salt** l'erreur
        (connexion, export, import), donc il ne peut pas etre reconstruit
        apres coup par la couche qui ne fait que l'afficher.
        """
        adapter = FakeAdapter(
            check_connection=ConnectionError_("ORA-12541", hint="Ecouter le port.")
        )
        r = preflight.check_connection("source", adapter)
        self.assertIn("Ecouter le port.", r.hint)

    def test_une_base_fermee_refuse_la_duplication(self):
        """Une instance en `MOUNT` accepte des connexions mais rien d'autre.

        L'export demarre, produit un dump, et l'import echoue sur une
        base non ouverte. Le refuser ici evite des heures de travail.
        """
        adapter = FakeAdapter(_connect_info={
            "version": "19.0.0.0.0", "instance": "OEMCC", "status": "MOUNT",
            "open_mode": "", "cdb": "NO", "session_user": "SYS", "host": "h",
        })
        r = preflight.check_connection("cible", adapter)
        self.assertEqual(r.status, FAIL)
        self.assertIn("MOUNT", r.message)

    def test_une_version_pas_19c_avertit_sans_bloquer(self):
        """Une base 21c est un avertissement, pas un refus.

        Un dump 19c s'importe dans une base plus recente ; refuser
        priverait l'outil de son usage le plus courant, qui est de
        faire descendre la duplication. A l'inverse, une base 11g
        **importerait** un dump 19c, et l'avertissement le dit.
        """
        adapter = FakeAdapter(_connect_info={
            "version": "21.0.0.0.0", "instance": "X", "status": "OPEN",
            "open_mode": "READ WRITE", "cdb": "NO", "session_user": "SYS",
            "host": "h",
        })
        r = preflight.check_connection("source", adapter)
        self.assertEqual(r.status, WARN)
        self.assertEqual(r.code, ec.PREREQ)

    def test_les_rus_19c_ne_declenchent_pas_l_avertissement(self):
        """19.0, 19.3 et 19.20 sont tous 19c.

        Comparer la chaine entiere ferait echouer la validation sur
        toutes les installations appliquees par RUs, c'est-a-dire
        presque toutes les bases de production.
        """
        for version in ("19.0.0.0.0", "19.3.0.0.0", "19.20.0.0.0", "19.22.0.0.0"):
            with self.subTest(version=version):
                self.assertTrue(preflight.is_19c(version))
                adapter = FakeAdapter(_connect_info={
                    "version": version, "instance": "X", "status": "OPEN",
                    "open_mode": "READ WRITE", "cdb": "NO",
                    "session_user": "SYS", "host": "h",
                })
                r = preflight.check_connection("source", adapter)
                self.assertEqual(r.status, OK, version)


class TestScenario02ConnexionCibleIndisponible(unittest.TestCase):
    """« connexion cible indisponible »

    Le scenario 1 le couvre au niveau du controle ; ce qui compte ici
    est que les deux cotes sont traites **symetriquement**, pour que le
    message d'un echec cible ne parle pas de la source.
    """

    def test_le_message_nomme_le_bon_cote(self):
        for cote in ("source", "cible"):
            with self.subTest(cote=cote):
                adapter = FakeAdapter(
                    check_connection=ConnectionError_("ORA-12514: host unknown")
                )
                r = preflight.check_connection(cote, adapter)
                self.assertIn(cote, r.name)

    def test_le_libelle_du_cote_est_ce_du_rapport(self):
        """`source` et `cible`, et non `SOURCE`/`TARGET`.

        Le rapport dit « connexion source » ; un controle nomme
        autrement impose a l'exploitant de traduire entre le rapport et
        la configuration, ce qui est la source classique d'une erreur de
        diagnosticlee.
        """
        r = preflight.check_connection("cible", FakeAdapter())
        self.assertTrue(r.name.startswith("connexion cible"))


class TestScenario03SchemaInexistant(unittest.TestCase):
    """« schema inexistant »"""

    def test_schema_source_absent(self):
        r = preflight.check_source_schema(
            FakeAdapter(schema_exists=False), "NOPE", content="ALL"
        )
        self.assertEqual(r.status, FAIL)
        self.assertIn("n'existe pas", r.message)

    def test_schema_cible_absent(self):
        """Le controle cible ne doit jamais presumer la creation du compte.

        Creer un schema est une operation d'initialisation de base, avec
        ses tablespaces, ses quotas et ses grants. La faire ici en
        silence produirait un compte que personne n'a voulu.
        """
        r = preflight.check_target_schema(
            FakeAdapter(schema_exists=False), "NOPE", allow_existing=False
        )
        self.assertEqual(r.status, FAIL)
        self.assertIn("n'existe pas", r.message)
        self.assertIn("ne cree pas de schema", r.hint)

    def test_le_remede_donne_la_requete_de_verification(self):
        """Un remede sans commande a executer n'est pas un remede.

        L'exploitant doit pouvoir copier-coller la requete sans
        construire lui-meme le dictionnaire.
        """
        r = preflight.check_source_schema(
            FakeAdapter(schema_exists=False), "NOPE", content="ALL"
        )
        self.assertIn("dba_users", r.hint)

    def test_un_schema_vide_est_refuse_a_la_source(self):
        """Un schema source vide produirait un dump vide reussi.

        L'import « reussirait » sans creer rien, et le rapport
        annoncerait une duplication faite. C'est un faux succes, donc
        un refus.
        """
        r = preflight.check_source_schema(
            FakeAdapter(schema_exists=True, object_count=0), "VIDE", content="ALL"
        )
        self.assertEqual(r.status, FAIL)
        self.assertIn("aucun objet", r.message)

    def test_un_schema_cible_vide_est_accepte(self):
        """A la cible, un schema vide est l'etat **voulu**.

        Dupliquer HR vers un compte UAT neuves est le cas d'usage
        principal ; refuser un schema cible vide reviendrait a interdire
        l'outil dans sa raison d'etre.
        """
        r = preflight.check_target_schema(
            FakeAdapter(schema_exists=True, object_count=0), "NEUF", allow_existing=False
        )
        self.assertEqual(r.status, OK)


class TestScenario04TablespaceInexistant(unittest.TestCase):
    """« tablespace inexistant »"""

    def test_un_tablespace_absent_est_refuse(self):
        results = preflight.check_tablespaces(
            FakeAdapter(tablespaces_capacity={"USERS": {"bytes": GO, "free_bytes": GO}}),
            "cible", ["USERS", "MANQUANT"], content="ALL",
        )
        echecs = [r for r in results if r.status == FAIL]
        self.assertEqual(len(echecs), 1)
        self.assertIn("MANQUANT", echecs[0].message)
        self.assertNotIn("USERS", echecs[0].message)

    def test_la_comparaison_des_noms_est_insensible_a_la_casse(self):
        """`DBA_FREE_SPACE` rend les noms en majuscules.

        Comparer exactement ferait echouer un `REMAP_TABLESPACE=users`
        ecrit en minuscules, alors que la base l'a creee en majuscules —
        soit un refus a tort sur une configuration valide.
        """
        results = preflight.check_tablespaces(
            FakeAdapter(tablespaces_capacity={"USERS": {"bytes": GO, "free_bytes": GO}}),
            "cible", ["users"], content="ALL",
        )
        self.assertEqual([r.status for r in results], [OK])

    def test_le_remede_dit_que_l_outil_ne_cree_pas(self):
        results = preflight.check_tablespaces(
            FakeAdapter(tablespaces_capacity={}),
            "cible", ["MANQUANT"], content="ALL",
        )
        self.assertIn("ne les cree pas", results[0].hint)

    def test_aucun_tablespace_impose_ne_perturbe_pas(self):
        """Sans `REMAP_TABLESPACE`, les tablespaces viennent du dump.

        Les decouvrir a l'import est le comportement normal ; en faire
        une erreur de configuration decourageait de configurer, donc de
        controler.
        """
        results = preflight.check_tablespaces(
            FakeAdapter(), "cible", [], content="ALL"
        )
        self.assertEqual([r.status for r in results], [SKIP])

    def test_les_mesures_sont_reportees_dans_le_resultat(self):
        """Le rapport doit porter les chiffres, pas seulement un « OK ».

        C'est ce qui permet de verifier a posteriori qu'un run est passe
        avec de la marge, ou avec trois minutes de marge.
        """
        results = preflight.check_tablespaces(
            FakeAdapter(tablespaces_capacity={"USERS": {"bytes": 4 * GO,
                                                        "free_bytes": 3 * GO}}),
            "cible", ["USERS"], content="ALL",
        )
        self.assertEqual(results[0].data["USERS"]["free_bytes"], 3 * GO)


class TestScenario05EspaceInsufficient(unittest.TestCase):
    """« espace insuffisant »"""

    def test_place_insuffisante_au_tablespace(self):
        results = preflight.check_tablespace_capacity(
            FakeAdapter(tablespaces_capacity={"USERS": {"bytes": 10 * GO,
                                                        "free_bytes": 100 * MO}}),
            "cible", "DP_DIR", 10 * GO,
            margin_percent=10, margin_abs_bytes=0,
        )
        self.assertEqual([r.status for r in results], [FAIL])
        self.assertIn("insuffisante", results[0].message)

    def test_marge_de_securite_appliquee(self):
        """La marge est ce qui distingue « juste » de « tenable ».

        Remplir un tablespace a 100 % laisse la creation des segments
        (extents, PCTFREE) sans espace, et l'import echoue en cours de
        route, apres avoir transfere le dump.
        """
        # 1 Go libres pour 1 Go demandes : sans marge, cela passe.
        adapter = FakeAdapter(tablespaces_capacity={"USERS": {"bytes": 2 * GO,
                                                              "free_bytes": GO}})
        sans_marge = preflight.check_tablespace_capacity(
            adapter, "cible", "DP", GO, margin_percent=0, margin_abs_bytes=0
        )
        self.assertEqual([r.status for r in sans_marge], [OK])
        # Avec 20 % de marge, la meme place est refusee.
        avec_marge = preflight.check_tablespace_capacity(
            adapter, "cible", "DP", GO, margin_percent=20, margin_abs_bytes=0
        )
        self.assertEqual([r.status for r in avec_marge], [FAIL])

    def test_les_marges_s_additionnent(self):
        """Marge relative **et** absolue : une seule des deux ne suffit pas.

        Sur un petit schema, 20 % represente quelques Mo — trop peu pour
        absorber la creation des index. La marge absolue couvre ce cas.
        """
        adapter = FakeAdapter(tablespaces_capacity={"USERS": {"bytes": 2 * GO,
                                                              "free_bytes": 100 * MO}})
        results = preflight.check_tablespace_capacity(
            adapter, "cible", "DP", 10 * MO,
            margin_percent=0, margin_abs_bytes=500 * MO,
        )
        self.assertEqual([r.status for r in results], [FAIL])

    def test_l_ecart_manquant_est_chiffre(self):
        """Dire « il manque de la place » sans dire combien est incomplet.

        L'exploitant doit pouvoir savoir s'il etend le tablespace de
        1 Go ou de 1 To sans rouvrir le calcul.
        """
        adapter = FakeAdapter(tablespaces_capacity={"USERS": {"bytes": 2 * GO,
                                                              "free_bytes": 100 * MO}})
        r = preflight.check_tablespace_capacity(
            adapter, "cible", "DP", 10 * GO, margin_percent=0, margin_abs_bytes=0
        )[0]
        self.assertEqual(r.data["shortfall"], 10 * GO - 100 * MO)

    def test_place_non_mesurable_avertit_sans_bloquer(self):
        """Sans acces a `DBA_FREE_SPACE`, on ne sait pas — on ne bloque pas.

        Un compte d'exploitation restreint est courant. Refuser sur une
        incertitude rendrait l'outil inutilisable sur ces bases, alors
        que l'import peut parfaitement reussir.
        """
        adapter = FakeAdapter(tablespaces_capacity={})
        r = preflight.check_tablespace_capacity(
            adapter, "cible", "DP", 10 * GO, margin_percent=10,
            margin_abs_bytes=0,
        )[0]
        self.assertEqual(r.status, WARN)
        self.assertEqual(r.code, ec.PREREQ)

    def test_place_non_mesurable_par_permission_n_est_pas_un_echec_sur(self):
        adapter = FakeAdapter(tablespaces_capacity=PrereqError("ORA-00942"))
        r = preflight.check_tablespace_capacity(
            adapter, "cible", "DP", 10 * GO, margin_percent=10, margin_abs_bytes=0
        )[0]
        self.assertEqual(r.status, FAIL)
        self.assertIn("DBA_FREE_SPACE", r.hint)

    def test_espace_disque_non_mesurable_est_signale_sans_bloquer(self):
        """Un disque non mesure n'est pas un disque plein.

        La confusion des deux ferait echouer des runs parfaitement
        viables sur un serveur ou la mesure n'est pas disponible.
        """
        results = preflight.check_space(
            FakeAdapter(), "cible", "DP_DIR", "/dpo", required_bytes=10 * GO,
            margin_percent=10, margin_abs_bytes=0, remote_free_bytes=None,
            content="ALL",
        )
        disque = [r for r in results if r.name.startswith("espace disque")][0]
        self.assertEqual(disque.status, SKIP)

    def test_disque_plein_est_un_echec(self):
        results = preflight.check_space(
            FakeAdapter(), "cible", "DP_DIR", "/dpo", required_bytes=10 * GO,
            margin_percent=10, margin_abs_bytes=0, remote_free_bytes=MO,
            content="ALL",
        )
        disque = [r for r in results if r.name.startswith("espace disque")][0]
        self.assertEqual(disque.status, FAIL)

    def test_metadonnees_seules_ignorent_le_tablespace(self):
        """En `METADATA_ONLY`, aucune donnee n'arrive : le tablespace ne grossit pas.

        Exiger la place des segments alors que le dump ne porte que le
        DDL ferait echouer une operation parfaitement faisable — et
        `CONTENT` existe justement pour la rendre faisable.
        """
        results = preflight.check_space(
            FakeAdapter(tablespaces_capacity={"USERS": {"bytes": 10 * GO,
                                                        "free_bytes": 0}}),
            "cible", "DP", "/d", required_bytes=10 * GO, margin_percent=10,
            margin_abs_bytes=0, remote_free_bytes=10 * GO,
            content="METADATA_ONLY",
        )
        ts = [r for r in results if r.name.startswith("espace tablespace")][0]
        self.assertEqual(ts.status, SKIP)
        self.assertIn("non pertinent", ts.message)

    def test_la_reduction_de_taille_est_faite_par_l_appelant(self):
        """`check_space` ne rediscount pas ce que le pipeline a deja estime.

        Un second coefficient ici serait **multiplicatif** avec celui du
        pipeline : le facteur reel, lui, deviendrait invisible et
        personne ne pourrait dire a quoi correspond le chiffre affiche.
        L'estimation est donc faite une fois, a un seul endroit, et
        `check_space` n'ajoute que la marge.
        """
        base = dict(
            required_bytes=1 * GO, margin_percent=10, margin_abs_bytes=0,
            remote_free_bytes=1 * GO,
        )
        results = preflight.check_space(
            FakeAdapter(), "cible", "DP", "/d", content="ALL", **base
        )
        disque = [r for r in results if r.name.startswith("espace disque")][0]
        # 1 Go + 10 %, et rien d'autre.
        self.assertEqual(disque.data["needed"], int(1.1 * GO))

    def test_le_libeille_dit_d_ou_vient_le_chiffre(self):
        """Une estimation dont on ne peut pas dire l'origine ne se discute pas.

        Le rapport doit indiquer sur quoi repose le calcul, pour qu'un
        exploitant puisse le contester utilement au lieu de le prendre
        pour argent paye.
        """
        for content, attendu in (("ALL", "segments du schema"),
                                  ("METADATA_ONLY", "metadonnees seules")):
            with self.subTest(content=content):
                results = preflight.check_space(
                    FakeAdapter(), "cible", "DP", "/d", required_bytes=1 * GO,
                    margin_percent=0, margin_abs_bytes=0,
                    remote_free_bytes=10 * GO, content=content,
                )
                disque = [r for r in results if r.name.startswith("espace disque")][0]
                self.assertIn(attendu, disque.message)

    def test_min_free_bytes_est_un_plancher_indispensable(self):
        """Un plancher absolu prime sur l'estimation.

        Un schema minuscule n'echange rien au besoin d'espace, mais un
        disque a 12 Mo libres echouera des que le dump y touchera. Le
        plancher existe pour cela.
        """
        results = preflight.check_space(
            FakeAdapter(), "cible", "DP", "/d", required_bytes=1 * MO,
            margin_percent=0, margin_abs_bytes=0, min_free_bytes=5 * GO,
            remote_free_bytes=2 * GO, content="ALL",
        )
        disque = [r for r in results if r.name.startswith("espace disque")][0]
        self.assertEqual(disque.status, FAIL)
        self.assertEqual(disque.data["needed"], 5 * GO)


class TestScenario10ObjetExistant(unittest.TestCase):
    """« objet existant » — le garde-fou central de l'outil."""

    def test_cible_peuplee_refusee_par_defaut(self):
        """Le refus est le comportement **par defaut**, pas une option.

        L'inverse — un ecrasement possible sans demande explicite —
        ferait dependre la conservation des donnees de la bonne
        lecture d'une ligne de configuration.
        """
        r = preflight.check_target_schema(
            FakeAdapter(schema_exists=True, object_count=42),
            "UAT", allow_existing=False,
        )
        self.assertEqual(r.status, FAIL)
        self.assertEqual(r.code, ec.SECURITY)

    def test_cible_peuplee_acceptee_en_avertissement_si_assumee(self):
        r = preflight.check_target_schema(
            FakeAdapter(schema_exists=True, object_count=42),
            "UAT", allow_existing=True,
        )
        self.assertEqual(r.status, WARN)
        self.assertIn("assume", r.message)

    def test_un_avertissement_ne_donne_pas_le_code_de_succes(self):
        """Le code reste 2 : l'exploitant doit voir qu'il y a un reserve.

        Renvoyer 0 ferait dire a l'ordonnanceur que la cible etait
        propre, alors qu'elle ne l'est pas.
        """
        r = preflight.check_target_schema(
            FakeAdapter(schema_exists=True, object_count=42),
            "UAT", allow_existing=True,
        )
        self.assertNotEqual(r.code, ec.SUCCESS)

    def test_le_remede_énumère_les_trois_issues(self):
        """Un remede doit couvrir les choix plausibles, pas un seul.

        L'exploitant qui voit « ALLOW_EXISTING_TARGET=true » sans la
        mention de `TABLE_EXISTS_ACTION` risque de l'ajouter et de
        decouvrir ensuite que `REPLACE` exige aussi l'option
        destructive.
        """
        r = preflight.check_target_schema(
            FakeAdapter(schema_exists=True, object_count=42),
            "UAT", allow_existing=False,
        )
        for attendu in ("ALLOW_EXISTING_TARGET", "TABLE_EXISTS_ACTION",
                        "--allow-destructive"):
            with self.subTest(attendu=attendu):
                self.assertIn(attendu, r.hint)

    def test_la_connexion_est_verifiee_une_seule_fois_par_cote(self):
        """L'etat de verification de l'adaptateur doit etre conserve.

        Reconstruire l'adaptateur a chaque appel re-testait la connexion
        a chaque controle — trois allers-retours SQL*Plus par etape, et
        un dump d'un quart d'heure rendu quatre fois plus lent.
        """
        adapter = FakeAdapter()
        preflight.check_connection("source", adapter)
        preflight.check_source_schema(adapter, "SRC", content="ALL")
        self.assertTrue(adapter.verified)


class TestScenario12PrivilegesInsuffisants(unittest.TestCase):
    """« privileges insuffisants »"""

    def test_role_complet_accepte(self):
        adapter = FakeAdapter(responses={"exp_full_database": [["EXP_FULL_DATABASE"]]})
        r = preflight.check_privileges(adapter, "source", for_export=True)
        self.assertEqual(r.status, OK)

    def test_acces_au_directory_seul_avertit(self):
        """Data Pump limite alors l'export au schema du compte connecte.

        Ce n'est pas un blocage, mais c'est une restriction de
        perimetre : l'operateur doit le savoir **avant** l'export.
        """
        adapter = FakeAdapter(responses={"read,write on directory": [
            ["READ,WRITE ON DIRECTORY"],
        ]})
        r = preflight.check_privileges(adapter, "source", for_export=True)
        self.assertEqual(r.status, WARN)
        self.assertIn("limite au schema", r.hint)

    def test_le_role_est_accepte_quelle_qu_en_soit_sa_graphie(self):
        """`SESSION_PRIVS` ecrit le role avec des espaces, pas un tiret bas.

        Oracle restitue `EXP_FULL_DATABASE` sous la forme `EXPORT FULL
        DATABASE`. Comparer la chaine documentee a la chaine Oracle
        rendait le controle negatif sur une session pourtant dotee --
        defaut observe sur une vraie base 19c, et invisible pour un jeu
        de tests qui renvoie l'identique dans les deux sens. Les deux
        graphies doivent donc etre acceptees.
        """
        for graphie in ("EXP_FULL_DATABASE", "EXPORT FULL DATABASE",
                        "exp_full_database", "  EXPORT FULL DATABASE  "):
            with self.subTest(graphie=graphie):
                adapter = FakeAdapter(
                    responses={"session_privs": [[graphie]]})
                r = preflight.check_privileges(adapter, "source", for_export=True)
                self.assertEqual(r.status, OK, f"refuse a tort : {graphie!r}")

    def test_le_role_d_export_ne_valide_pas_l_import(self):
        """La normalisation ne doit pas confondre les deux sens.

        `EXPORT FULL DATABASE` contient « FULL DATABASE » comme
        `IMPORT FULL DATABASE` : une comparaison trop laxiste
        validerait un compte incapable d'importer. C'est le piege
        direct du correctif ci-dessus.
        """
        adapter = FakeAdapter(
            responses={"session_privs": [["EXPORT FULL DATABASE"]]})
        r = preflight.check_privileges(adapter, "cible", for_export=False)
        self.assertNotEqual(r.status, OK)
        self.assertIn("IMP_FULL_DATABASE", r.message)

    def test_le_privilege_de_directoire_est_accepte_avec_ses_deux_graphies(self):
        """`READ,WRITE ON DIRECTORY` est un privilege objet, pas un role.

        Meme raison que ci-dessus : la vue le restitue avec des espaces
        la ou la documentation ecrit un tiret bas, et le compte qui
        n'a que ce privilege peut bel et bien dupliquer.
        """
        for graphie in ("READ,WRITE ON DIRECTORY", "READ,WRITE_ON_DIRECTORY"):
            with self.subTest(graphie=graphie):
                adapter = FakeAdapter(responses={"session_privs": [[graphie]]})
                r = preflight.check_privileges(adapter, "source", for_export=True)
                self.assertEqual(r.status, WARN, f"refuse a tort : {graphie!r}")

    def test_aucun_privilege_refuse(self):
        r = preflight.check_privileges(FakeAdapter(), "cible", for_export=False)
        self.assertEqual(r.status, FAIL)
        self.assertIn("IMP_FULL_DATABASE", r.message)

    def test_un_privilege_ilisible_refuse_avec_un_remede(self):
        """`ORA-00942` sur `SESSION_PRIVS` ne vient pas du compte.

        La vue est accessible a tout compte connecte : son echec de
        lecture ne dit pas « privileges insuffisants » mais « controle
        impossible ». Le dire «ORA-00942» n'aiderait personne ; nommer la
        vue a interroger donne l'action.
        """
        adapter = FakeAdapter(responses={"session_privs": PrereqError("ORA-00942")})
        r = preflight.check_privileges(adapter, "source", for_export=True)
        self.assertEqual(r.status, FAIL)
        self.assertIn("EXP_FULL_DATABASE", r.hint)

    def test_export_et_import_cherchent_des_privileges_differents(self):
        """Un compte d'export ne peut pas automatiquement importer.

        Verifier le mauvais privilege donnerait un avertissement sur un
        run d'export valide, et laisserait passer l'import sans les
        droits necessaires.
        """
        export = FakeAdapter(responses={"exp_full_database": [["EXP_FULL_DATABASE"]]})
        r = preflight.check_privileges(export, "source", for_export=True)
        self.assertEqual(r.status, OK)
        # Le meme compte, mais pour un import : pas le privilege attendu.
        r2 = preflight.check_privileges(export, "cible", for_export=False)
        self.assertNotEqual(r2.status, OK)


class TestScenario13ErreurOracle(unittest.TestCase):
    """« erreur Oracle » — un code doit toujours etre produit."""

    def test_une_erreur_inconnue_remonte_telle_quelle(self):
        """Une exception hors `OsdError` ne doit etre ni absorbee ni codee 0.

        C'est le piege le plus grave du projet : une exception non
        capturee qui vaudrait « pas d'erreur » ferait sortir l'outil en
        succes apres un echec reel. Le controle ne peut donc pas
        catcher large — c'est `main()`, qui a le contexte, qui
        normalise en code de sortie, et qui doit conserver le
        traceback.
        """
        adapter = FakeAdapter(check_connection=RuntimeError("bogue interne"))
        with self.assertRaises(RuntimeError):
            preflight.check_connection("source", adapter)

    def test_une_osderror_est_bien_translatee_en_code(self):
        """A l'inverse, une `OsdError` **doit** etre traduite.

        Le controle laite si l'exception portant un code se propageait :
        `main()` recevrait une erreur technique au lieu du code 3 prevu
        pour une connexion, et l'ordonnanceur ne pourrait pas distinguer
        les deux.
        """
        adapter = FakeAdapter(
            check_connection=ConnectionError_("ORA-12541")
        )
        r = preflight.check_connection("source", adapter)
        self.assertEqual(r.code, ec.CONNECTION)

    def test_les_codes_ora_sont_reportes_tels_quels(self):
        """Le code Oracle est la piece d'information la plus utile.

        Il est documente, donc exploitable dans un ticket, et il n'est
        jamais reformule : un `ORA-12541` se reconnait a la relecture.
        """
        adapter = FakeAdapter(
            check_connection=ConnectionError_("ORA-01017: invalid credentials")
        )
        r = preflight.check_connection("source", adapter)
        self.assertIn("ORA-01017", r.message)


class TestScenario03BisSchemaSourceVideEtInvalide(unittest.TestCase):
    """Objets invalides : un avertissement, pas un refus."""

    def test_des_objets_invalides_avertissent(self):
        """Data Pump exporte un objet invalide sans echouer.

        Le refuser ici bloquerait la duplication sur un defaut de la
        **source**, que l'outil n'a pas vocation a corriger. L'avertir
        laisse l'exploitant decider si l'integrite fait partie du
        besoin — et le signale avant qu'un invalide apparaisse aussi
        cote cible, ou il sera difficile a attribuer.
        """
        r = preflight.check_source_schema(
            FakeAdapter(
                schema_exists=True,
                object_count={"": 30, "INVALID": 2},
            ),
            "SRC", content="ALL",
        )
        self.assertEqual(r.status, WARN)
        self.assertIn("2 objet(s) invalide(s)", r.message)
        self.assertEqual(r.code, ec.PREREQ)

    def test_sans_objet_invalide_le_controle_reste_vert(self):
        r = preflight.check_source_schema(
            FakeAdapter(schema_exists=True, object_count={"": 30, "INVALID": 0}),
            "SRC", content="ALL",
        )
        self.assertEqual(r.status, OK)
        self.assertEqual(r.data["objects"], 30)


class TestRegroupement(unittest.TestCase):
    """`worst`, `first_failure` et `_merge` portent la decision finale."""

    def test_worst_prend_la_severite_la_plus_forte(self):
        results = [
            preflight.CheckResult("a", status=OK),
            preflight.CheckResult("b", status=WARN),
            preflight.CheckResult("c", status=FAIL),
        ]
        self.assertEqual(preflight.worst(results), FAIL)
        self.assertEqual(preflight.worst(results[:2]), WARN)
        self.assertEqual(preflight.worst(results[:1]), OK)
        self.assertEqual(preflight.worst([]), OK)

    def test_worst_classe_skip_entre_ok_et_warn(self):
        """`SKIP` est « non evaluable », pas « bon ».

        Le placer au niveau de `OK` ferait passer une verification
        escamotee pour une verification reussie.
        """
        self.assertEqual(
            preflight.worst([
                preflight.CheckResult("a", status=OK),
                preflight.CheckResult("b", status=SKIP),
            ]),
            SKIP,
        )

    def test_first_failure_respecte_l_ordre(self):
        """Le premier echec est la cause la plus en amont.

        Inverser l'ordre ferait diagnostiquer un tablespace manquant
        alors que la cause est un schema inexistant.
        """
        results = [
            preflight.CheckResult("connexion", status=OK),
            preflight.CheckResult("schema source", status=FAIL, code=ec.PREREQ),
            preflight.CheckResult("tablespace", status=FAIL, code=ec.PREREQ),
        ]
        self.assertEqual(preflight.first_failure(results).name, "schema source")

    def test_first_failure_renvoie_none_si_tout_passe(self):
        self.assertIsNone(
            preflight.first_failure([preflight.CheckResult("a", status=OK)])
        )

    def test_la_cause_du_refus_est_consignee_dans_le_resultat(self):
        """Le message de l'echec doit nommer l'echec interne.

        Sans cela, `first_failure` ne pourrait pas distinguer « la
        connexion a echoue » d'« un tablespace manque » en ne regardant
        que le controle englobant.
        """
        adapter = FakeAdapter(
            check_connection=ConnectionError_("ORA-12541: no listener")
        )
        r = preflight.check_connection("source", adapter)
        self.assertIn("ORA-12541", r.message)
        self.assertEqual(r.code, ec.CONNECTION)


class TestRendu(unittest.TestCase):
    def test_le_rendu_ne_depend_d_aucune_couleur(self):
        """Le rapport part par mail et par la sortie de cron.

        Des codes ANSI dans le rendu produiraient du `ESC[32m` litteral
        dans le journal, illisible et impossible a comparer entre deux
        runs.
        """
        r = preflight.CheckResult("connexion source", status=FAIL,
                                  message="ORA-12541", hint="Verifier le port.")
        lignes = r.to_lines()
        self.assertTrue(all("\x1b" not in ligne for ligne in lignes))

    def test_le_remede_n_apparait_qu_en_cas_de_probleme(self):
        """Un remediation sur un controle reussi est du bruit.

        Le rapport liste une绝对不是-quarante controles : afficher une
        ligne « remed(e) » sous chaque `OK` noierait l'information utile.
        """
        ok = preflight.CheckResult("a", status=OK, hint="inutile ici")
        self.assertFalse(any("remed" in ligne for ligne in ok.to_lines()))
        ko = preflight.CheckResult("b", status=WARN, hint="faire ceci")
        self.assertTrue(any("remed" in ligne for ligne in ko.to_lines()))

    def test_le_rendu_est_serialisable_en_json(self):
        """Le rapport JSON consomme `to_dict()`.

        Un `to_dict` incomplet — typiquement un `data` non reporte —
        ferait perdre au rapport machine les chiffres que le rapport
        texte sait, lui, afficher.
        """
        r = preflight.CheckResult("espace", status=FAIL, code=ec.PREREQ,
                                  message="insuffisant", data={"shortfall": 42})
        import json

        d = json.loads(json.dumps(r.to_dict()))
        self.assertEqual(d["data"]["shortfall"], 42)
        self.assertEqual(d["code"], ec.PREREQ)
        self.assertIn("detail", d)
        self.assertIn("hint", d)


if __name__ == "__main__":
    unittest.main()
