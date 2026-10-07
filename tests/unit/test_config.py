"""Tests du chargement et de la validation de la configuration.

La configuration est la premiere ligne de defense de l'outil : c'est la
seule entree qui ne vient ni d'Oracle ni de l'exploitant, et une erreur
de lecture y devient une erreur d'ecriture en base. Ces tests couvrent
donc trois choses : le refus de ce qui est dangereux, l'acceptation de ce
qui est legitime, et le refus de ce qui serait **accepte et ignore** —
bien plus piege qu'un refus franc, car il laisse croire a un comportement
qui n'existe pas.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

import support

from osd import config as cfg_mod
from osd.errors import ConfigError


class TestParseurConf(unittest.TestCase):
    """Le fichier `.conf` est une entree non fiable : il n'est pas execute.

    `parse_conf` valide **contre le schema** : c'est lui qui rend le
    fichier « valide en totalite ou refuse », donc les tests ne peuvent
    pas employer des cles fictives. Toutes les cles employees ici
    existent donc reellement dans `SCHEMA`.
    """

    def test_lit_cle_valeur(self):
        raw = cfg_mod.parse_conf(
            "PARALLEL=8\nLOG_LEVEL=DEBUG\nSOURCE_USER='trois'\n"
        )
        self.assertEqual(
            raw, {"PARALLEL": "8", "LOG_LEVEL": "DEBUG", "SOURCE_USER": "trois"}
        )

    def test_retire_les_quotes_de_chaque_cote(self):
        """Un chemin de wallet colle en `'...'` ne doit pas le garder.

        Le guillemet survivrait dans la chaine et produirait un chemin
        inexistant, avec un message d'erreur qui ne parlerait que du
        fichier absent.
        """
        raw = cfg_mod.parse_conf(
            "SOURCE_TNS_ADMIN='/opt/oracle/network/admin'\n"
            'TARGET_TNS_ADMIN="/opt/oracle/network/admin"\n'
        )
        self.assertEqual(raw["SOURCE_TNS_ADMIN"], "/opt/oracle/network/admin")
        self.assertEqual(raw["TARGET_TNS_ADMIN"], "/opt/oracle/network/admin")

    def test_ignore_commentaires_et_blancs(self):
        raw = cfg_mod.parse_conf(
            "# commentaire\n\n  PARALLEL=2  \n   # autre\nCOMPRESSION=ALL\n"
        )
        self.assertEqual(raw, {"PARALLEL": "2", "COMPRESSION": "ALL"})

    def test_refuse_une_cle_dupliquee(self):
        """La derniere ecrasee-t-elle, ou les deux sont-elles un conflit ?

        Un fichier de configuration est lu par des humains et edite a la
        main ; deux lignes pour la meme cle(signent presque toujours
        une fusion de fichiers oubliee. Accepter silencieusement la
        derniere reviendrait a appliquer un choix que personne n'a fait
        consciemment.
        """
        with self.assertRaises(ConfigError) as ctx:
            cfg_mod.parse_conf("PARALLEL=2\nPARALLEL=8\n")
        self.assertIn("PARALLEL", str(ctx.exception))

    def test_refuse_les_constructions_shell(self):
        """Un fichier de configuration ne doit pas pouvoir executer du code.

        `source`/`eval` d'un fichier tiers reviendrait a executer du code
        arbitraire avec les privileges de l'operateur. Le parseur refuse
        donc explicitement ce qui resemble a du shell, meme si le shell
        ne l'executerait pas : c'est une defense contre la *reutilisation*
        ulterieure du fichier par un `source`.
        """
        for ligne in (
            "A=$(id)",
            "A=`id`",
            "A=1; B=2",
            "A=1 && B=2",
            "A=1 || B=2",
            "export A=1",
            "A=1 > /tmp/f",
            "A=1 < /etc/passwd",
        ):
            with self.subTest(ligne=ligne):
                with self.assertRaises(ConfigError):
                    cfg_mod.parse_conf(ligne + "\n")

    def test_refuse_une_cle_inconnue(self):
        """Une faute de frappe silencieuse est la panne la plus longue a trouver.

        Un `PARALEL=4` ignore produit un run « reussi » qui n'a rien
        parallelise. L'echec doit etre immediat et nomme.
        """
        with self.assertRaises(ConfigError) as ctx:
            cfg_mod.parse_conf("PARALEL=4\n")
        self.assertIn("PARALEL", str(ctx.exception))

    def test_refuse_une_cle_sans_valeur_non_vide(self):
        with self.assertRaises(ConfigError):
            cfg_mod.parse_conf("PARALLEL=\n")

    def test_accepte_une_cle_vide_si_son_defaut_l_est(self):
        raw = cfg_mod.parse_conf("SOURCE_USER=\n")
        self.assertEqual(raw, {"SOURCE_USER": ""})


class TestChargement(unittest.TestCase):
    def test_les_defauts_du_schema_s_appliquent(self):
        cfg = support.load_config()
        self.assertEqual(cfg.get("PARALLEL"), 4)
        self.assertEqual(cfg.get("TABLE_EXISTS_ACTION"), "SKIP")
        self.assertEqual(cfg.get("ALLOW_EXISTING_TARGET"), False)
        self.assertEqual(cfg.get("DRY_RUN"), False)

    def test_un_defaut_csv_est_bien_une_liste(self):
        """Un defaut de liste reste une liste apres chargement.

        Le test portait sur `SOURCE_SSH_OPTS`, dont le defaut etait
        `BatchMode=yes,ConnectTimeout=10,...`. Cette cle est desormais
        retiree, mais la propriete qu'elle verifiait est generale et
        reste vraie : chargee comme chaine, une liste serait parcourue
        caractere par caractere, et chaque element insererait une
        virgule. L'erreur serait alors ailleurs -- un element invalide
        qu'aucun controle ne relie a la configuration de l'utilisateur.

        Il faut donc une cle CSV **vivante** pour que le test garde un
        sens. Toutes les cles CSV restantes ont un defaut vide, ce qui
        suffit : la propriete a verifier est le **type** rendu, pas la
        presence d'elements. Le defaut vide est le cas le plus
        trompeur, puisqu'une chaine vide se parcourt sans erreur et ne
        revele le defaut qu'a l'ecriture.
        """
        csv_vivantes = [
            cle for cle, spec in cfg_mod.SCHEMA.items() if spec.kind == "csv"
        ]
        # Les cles retirees restent typees `csv` pour qu'un fichier
        # existant soit lu, mais les renseigner declenche un refus. Les
        # ecarter ici evite de transformer une verification de typage en
        # test du refus, qui a son propre module.
        csv_vivantes = [c for c in csv_vivantes if c not in cfg_mod._OBSOLETE_SSH_KEYS]
        self.assertTrue(csv_vivantes, "aucune cle CSV vivante dans le schema")
        for cle in csv_vivantes:
            with self.subTest(cle=cle):
                # Le defaut reste une liste...
                self.assertIsInstance(support.load_config().get(cle), list)
                # ...et une valeur explicite aussi.
                self.assertEqual(
                    support.load_config(**{cle: "a,b,c"}).get(cle), ["a", "b", "c"]
                )

    def test_une_cle_vide_avec_defaut_emploie_le_defaut_et_signale(self):
        cfg = support.load_config(PARALLEL="")
        self.assertEqual(cfg.get("PARALLEL"), 4)
        self.assertTrue(any("PARALLEL" in w for w in cfg.warnings))

    def test_les_surcharges_cli_l_emportent(self):
        """Priorite : defauts < fichier < environnement < CLI.

        L'ordre est ce qui permet a un ordonnanceur de corriger une
        configuration partagee sans la modifier sur disque.
        """
        cfg = support.load_config(PARALLEL="8")
        self.assertEqual(cfg.get("PARALLEL"), 8)

    def test_fichier_absent_refuse(self):
        with self.assertRaises(ConfigError) as ctx:
            cfg_mod.load("/nonexistent/osd.conf")
        self.assertIn("config.example.conf", str(ctx.exception) + str(ctx.exception.hint))


class TestGardeFous(unittest.TestCase):
    def test_refuse_une_source_egale_a_la_cible(self):
        """Exporter puis importer dans le meme schema n'est pas une duplication.

        L'oubli du nom cible ne se verrait qu'apres l'avoir constate, et
        apres que `expdp` ait tourne.
        """
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(TARGET_SCHEMA="SRC")
        self.assertIn("identiques", str(ctx.exception))

    def test_signale_une_action_destructrice(self):
        """REPLACE et TRUNCATE sont signales comme destructifs.

        L'autorisation elle-meme n'est **pas** decidable au chargement :
        elle depend de l'option `--allow-destructive` de la ligne de
        commande, que la configuration ne voit pas. C'est l'etape 2 du
        pipeline qui refuse, avec le code 8.

        Ce qui se verifie ici est la *detection*, qui doit etre exacte :
        un faux negatif laisserait `REPLACE` passer pour sur, et
        l'echec ne se decouvrirait qu'apres l'ecrasement.
        """
        for action, attendu in (
            ("SKIP", False),
            ("APPEND", False),
            ("REPLACE", True),
            ("TRUNCATE", True),
        ):
            with self.subTest(action=action):
                cfg = support.load_config(TABLE_EXISTS_ACTION=action)
                destructif, raison = cfg.is_destructive()
                self.assertIs(destructif, attendu)
                if attendu:
                    self.assertIn(action, raison)
                else:
                    self.assertEqual(raison, "")

    def test_signale_cleanup_on_failure_comme_destructif(self):
        """Supprimer les artefacts d'un echec reste une destruction.

        Le vocabulaire « nettoyage » le fait oublier, et l'oubli coute un
        dump de plusieurs giga-octets et une reprise entiere a refaire.
        """
        cfg = support.load_config(CLEANUP_ON_FAILURE="true")
        destructif, raison = cfg.is_destructive()
        self.assertTrue(destructif)
        self.assertIn("CLEANUP_ON_FAILURE", raison)

    def test_accepte_append_qui_n_est_pas_destructif(self):
        """APPEND ajoute des lignes mais ne supprime rien.

        Il peut violer une cle unique — c'est un risque d'integrite, pas
        une destruction — donc le refus serait disproportionne.
        """
        cfg = support.load_config(TABLE_EXISTS_ACTION="APPEND")
        self.assertEqual(cfg.get("TABLE_EXISTS_ACTION"), "APPEND")

    def test_refuse_un_mot_de_passe_sans_wallet(self):
        """Le mot de passe en clair apparait dans `ps` et dans les journaux.

        Il est donc refuse, et pas « deprecie » : un avertissement
        serait ignore par une reflexe constante.
        """
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(PASSWORD="Secrete123")
        self.assertIn("wallet", str(ctx.exception).lower())

    def test_refuse_parallel_inferieur_a_un(self):
        with self.assertRaises(ConfigError):
            support.load_config(PARALLEL="0")

    def test_refuse_un_enum_inconnu(self):
        for cle, valeur in (
            ("CONTENT", "FULL"),
            ("TRANSFER_MODE", "carrier-pigeon"),
            ("VALIDATION_LEVEL", "MAX"),
            ("COMPRESSION", "SOME"),
        ):
            with self.subTest(cle=cle):
                with self.assertRaises(ConfigError):
                    support.load_config(**{cle: valeur})

    def test_refuse_une_cible_vide(self):
        with self.assertRaises(ConfigError):
            support.load_config(TARGET_SCHEMA="")

    def test_repertoire_vide_dans_un_fichier_refuse(self):
        """`LOG_DIR=` dans un fichier est une ligne oubliee, pas un choix.

        Le refus est ici et non un defaut applique : un fichier est
        edite a la main, une ligne tronquee ne peut pas avoir de
        volonte, et lui appliquer silencieusement `logs` enverrait les
        journaux ailleurs que prévu.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fichier = Path(tmp) / "a.conf"
            fichier.write_text("LOG_DIR=\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                cfg_mod.load(fichier, overrides=support.overrides())
            self.assertIn("LOG_DIR", str(ctx.exception))

    def test_repertoire_vide_en_surcharge_tombe_sur_le_defaut(self):
        """En revanche `--set LOG_DIR=` applique le defaut, avec un avertissement.

        La distinction est celle de la **source** de la valeur : une
        surcharge vient d'un ordonnanceur ou d'un environnement, qui
        cherche a *neutraliser* une valeur heritee, pas a la rendre
        invalide. Le comportement est uniforme sur toutes les cles, et
        l'avertissement dit ce qui a ete substitue.
        """
        cfg = support.load_config(LOG_DIR="")
        self.assertEqual(cfg.get("LOG_DIR"), "logs")
        self.assertTrue(any("LOG_DIR" in w for w in cfg.warnings))

    def test_refuse_un_schema_injectable(self):
        """Le nom de schema entre dans du SQL construit par concatenation.

        Un identifiant strict est donc aussi un garde-fou contre
        l'injection — pas seulement une contrainte de nommage.
        """
        for nom in ("HR; DROP TABLE X--", "HR' OR '1'='1", "A" * 31, "1HR", ""):
            with self.subTest(nom=nom):
                with self.assertRaises(ConfigError):
                    support.load_config(SOURCE_SCHEMA=nom)


class TestAuthentificationOS(unittest.TestCase):
    """`*_OS_AUTH` : la connexion par identite du systeme.

    Mesure sur les hotes : `/ as sysdba` repond, `/@alias as sysdba`
    est refuse en ORA-01017. L'authentification OS est **locale** — elle
    passe par `/`, pas par une chaine — et exclusive : `CONNECT` et
    `WALLET` sont deux formes de connexion qui la contredisent, et la
    combinaison echouerait apres coup, a une etape qui ne lit qu'un
    « ORA-01017 » sans lien avec la cause.
    """

    def test_os_auth_dispense_de_connect(self):
        """`/` remplace la chaine : la configuration est valide sans elle."""
        cfg = support.load_config(
            SOURCE_CONNECT="", TARGET_CONNECT="",
            SOURCE_OS_AUTH="true", TARGET_OS_AUTH="true",
        )
        self.assertTrue(cfg.get("SOURCE_OS_AUTH"))
        self.assertTrue(cfg.get("TARGET_OS_AUTH"))

    def test_les_deux_formes_coexistent_cote_a_cote(self):
        """`SOURCE` en OS ne change rien a la regle du `TARGET`.

        La validation compose par cote, elle n'imite pas : `SOURCE` peut
        etre en authentification OS pendant que `TARGET` reste regle par
        `CONNECT`, et l'inverse aussi.
        """
        cfg = support.load_config(SOURCE_CONNECT="", SOURCE_OS_AUTH="true")
        self.assertTrue(cfg.get("SOURCE_OS_AUTH"))
        self.assertEqual(cfg.get("SOURCE_CONNECT"), "")
        self.assertEqual(cfg.get("TARGET_CONNECT"), "L_TGT")
        self.assertFalse(cfg.get("TARGET_OS_AUTH"))
        self.assertFalse(cfg.get("SOURCE_WALLET"))

    def test_os_auth_refuse_une_chaine_connexion(self):
        """Chaine + OS : deux identites pour une connexion.

        Renseigner `SOURCE_CONNECT` avec `SOURCE_OS_AUTH=true` menait a
        un `ORA-01017` mesure — la chaine est ignoree par le client en
        mode OS, mais l'exploitant la croit active. Refuser nomme le
        conflit avant le run, la ou le rapport peut le porter.
        """
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(SOURCE_OS_AUTH="true")
        message = str(ctx.exception)
        self.assertIn("SOURCE_OS_AUTH=true est incompatible", message)
        self.assertIn("SOURCE_CONNECT", message)
        self.assertIn("locale", ctx.exception.hint)

    def test_os_auth_refuse_un_wallet(self):
        """Le wallet est une identite externe : elle contredit `/`."""
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(
                SOURCE_CONNECT="", SOURCE_OS_AUTH="true",
                SOURCE_WALLET="/opt/oracle/wallet",
            )
        self.assertIn("SOURCE_WALLET", str(ctx.exception))

    def test_la_dispense_joue_aussi_cote_cible(self):
        """La symetrie est une regle, pas un defaut du copier-coller."""
        cfg = support.load_config(
            TARGET_CONNECT="", TARGET_OS_AUTH="true",
        )
        self.assertTrue(cfg.get("TARGET_OS_AUTH"))
        self.assertEqual(cfg.get("SOURCE_CONNECT"), "L_SRC")

    def test_sans_os_auth_connect_reste_obligatoire(self):
        """La dispense est liee a l'option, pas a l'humeur du fichier."""
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(SOURCE_CONNECT="")
        self.assertIn("SOURCE_CONNECT absent", str(ctx.exception))


class TestClesObsoletes(unittest.TestCase):
    """Une option acceptee et ignoree est pire qu'une option refusee.

    Elle laisse croire a un comportement qui n'existe pas, et l'exploitant
    configure un outil qui ne fera pas ce qu'il croit.
    """

    def test_refuse_une_cle_obsolete_renseignee(self):
        for cle, valeur in (("STAGING_DIR", "/tmp/x"), ("REMOTE_TRANSFER", "true")):
            with self.subTest(cle=cle):
                with self.assertRaises(ConfigError) as ctx:
                    support.load_config(**{cle: valeur})
                self.assertIn(cle, str(ctx.exception))

    def test_accepte_une_cle_obsolete_vide(self):
        """Etre toleree a vide evite de casser un fichier de configuration existant."""
        cfg = support.load_config(STAGING_DIR="", REMOTE_TRANSFER="false")
        self.assertEqual(cfg.get("STAGING_DIR"), "")
        self.assertFalse(cfg.get("REMOTE_TRANSFER"))


class TestExecutionDistante(unittest.TestCase):
    """Execution distante : l'inventaire est la seule voie d'acces.

    Les regles ont change de forme, pas de fond. Le controle portait
    autrefois sur `*_SSH_OPTS` et sur la cle privee, tous deux pousses
    dans l'inventaire Ansible. Les garanties recherchees restent
    identiques et sont donc re-exprimees sur les nouveaux supports :

    * un hote designe sans inventaire exploitable doit etre refuse a la
      lecture, pas decouvert a l'etape 4 ;
    * un mot de passe de coffre lisible par d'autres doit etre refuse,
      pour la meme raison qu'une cle lisible par d'autres l'etait ;
    * une des six cles retirees doit etre refusee des qu'elle porte une
      valeur, sans quoi l'exploitant croirait qu'elle agit encore.
    """

    def test_refuse_un_inventaire_absent(self):
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(
                SOURCE_HOST="h", TARGET_HOST="h2",
                OSD_INVENTORY="/nonexistent/inventaire",
            )
        self.assertIn("OSD_INVENTORY", str(ctx.exception))
        self.assertIn("introuvable", str(ctx.exception))

    def test_refuse_un_hote_sans_mot_de_passe_de_coffre(self):
        """Sans le secret du coffre, Ansible ne peut rien dechiffrer.

        L'erreur doit nommer la cle manquante : c'est elle que
        l'exploitant doit ajouter, et le dire evite qu'il cherche du
        cote des identifiants, qui ne sont plus la.
        """
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(
                SOURCE_HOST="h", TARGET_HOST="h2", OSD_VAULT_PASSWORD_FILE=""
            )
        self.assertIn("OSD_VAULT_PASSWORD_FILE", str(ctx.exception))

    def test_refuse_un_mot_de_passe_de_coffre_absent(self):
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(
                SOURCE_HOST="h", TARGET_HOST="h2",
                OSD_VAULT_PASSWORD_FILE="/nonexistent/vault-pass",
            )
        self.assertIn("introuvable", str(ctx.exception))

    def test_refuse_un_mot_de_passe_de_coffre_trop_ouvert(self):
        """Un secret de coffre lisible par d'autres n'est pas un secret.

        Meme raison que pour la cle privee remplacee : le dire ici evite
        un echec d'Ansible dont le message ne parle que de
        permissions, ce qui oriente vers le mauvais probleme et fait
        perdre du temps sur un AIX de production.
        """
        with tempfile.TemporaryDirectory() as tmp:
            coffre = Path(tmp) / "vault-pass"
            coffre.write_text("secret\n")
            coffre.chmod(0o644)
            with self.assertRaises(ConfigError) as ctx:
                support.load_config(
                    SOURCE_HOST="h", TARGET_HOST="h2",
                    OSD_VAULT_PASSWORD_FILE=str(coffre),
                )
            self.assertIn("600", str(ctx.exception))

    def test_accepte_un_mot_de_passe_de_coffre_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            coffre = Path(tmp) / "vault-pass"
            coffre.write_text("secret\n")
            coffre.chmod(0o600)
            cfg = support.load_config(
                SOURCE_HOST="h", TARGET_HOST="h2",
                OSD_VAULT_PASSWORD_FILE=str(coffre),
            )
            self.assertEqual(cfg.get("OSD_VAULT_PASSWORD_FILE"), str(coffre))

    def test_refuse_une_cle_ssh_obsolete_renseignee(self):
        """Une cle retiree mais honoree laisserait croire a son effet.

        Le pire scenario n'est pas le refus : c'est l'acceptation
        silencieuse. L'exploitant aurait depose une cle, verrait le run
        echouer pour une autre raison, et ne soupconnerait pas que le
        secret n'a jamais ete lu. Le refus doit donc nommer la cle et
        dire ou elle va desormais.
        """
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "id_rsa"
            key.write_text("x")
            key.chmod(0o600)
            with self.assertRaises(ConfigError) as ctx:
                support.load_config(SOURCE_HOST="h", TARGET_HOST="h2", SSH_KEY=str(key))
            self.assertIn("SSH_KEY", str(ctx.exception))
            self.assertIn("n'est plus pris en charge", str(ctx.exception))
            self.assertIn("inventaire", str(ctx.exception.hint))

    def test_refuse_toutes_les_cles_ssh_obsoletes(self):
        """Les six cles retirees sont refusees, sans exception.

        Une seule qui serait encore acceptee suffirait a laisser un
        exploitant avec une configuration qui semble fonctionner et
        qui n'authentifie personne.
        """
        for cle in (
            "SSH_KEY",
            "OS_SSH_USER",
            "SOURCE_SSH_USER",
            "TARGET_SSH_USER",
            "SOURCE_SSH_OPTS",
            "TARGET_SSH_OPTS",
        ):
            with self.subTest(cle=cle):
                with self.assertRaises(ConfigError) as ctx:
                    support.load_config(SOURCE_HOST="h", TARGET_HOST="h2", **{cle: "quelque-chose"})
                self.assertIn(cle, str(ctx.exception))

    def test_accepte_les_cles_obsoletes_a_vide(self):
        """Vides, elles restent tolerees : un fichier existant reste lisible.

        C'est le meme compromis que pour `STAGING_DIR` : refuser une cle
        vide obligerait a editer le fichier de configuration a la main,
        alors que sa valeur est sans effet.
        """
        cfg = support.load_config(SOURCE_HOST="h", TARGET_HOST="h2", SSH_KEY="", OS_SSH_USER="")
        self.assertEqual(cfg.get("SSH_KEY"), "")
        self.assertEqual(cfg.get("OS_SSH_USER"), "")

    def test_execution_locale_n_exige_pas_d_inventaire(self):
        """Sans hote, il n'y a rien a authentifier : pas de coffre exige.

        Exiger l'inventaire en execution locale imposerait une
        dependance inutile au mode le plus simple, qui est aussi le mode
        de developpement. Le defaut est donc accepte tel quel : c'est
        l'absence d'inventaire exploitable qui compte, pas sa valeur.
        """
        cfg = support.load_config(
            SOURCE_HOST="", TARGET_HOST="",
            OSD_VAULT_PASSWORD_FILE=None, OSD_INVENTORY=None,
        )
        self.assertEqual(cfg.get("OSD_VAULT_PASSWORD_FILE"), "")


class TestTopologieMixte(unittest.TestCase):
    """Un cote local, un cote distant : refuse a la lecture.

    `SOURCE_HOST` vide est une configuration prevue — c'est le mode
    « le serveur de saut heberge la base ». La combiner avec un
    `TARGET_HOST` renseigne etait saisissable, et le chemin du transfert
    n'a pas de forme pour une source locale : le dump ecrit sur le
    serveur de saut n'est pas visible de l'hote distant, et
    l'import aurait echoue sur un `ORA-39000` **trois etapes plus loin**,
    avec pour seul indice un dump parfaitement valide cote source.
    """

    def test_refuse_source_distante_et_cible_locale(self):
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(SOURCE_HOST="h", TARGET_HOST="")
        self.assertIn("TARGET_HOST", str(ctx.exception))
        self.assertIn("TARGET_HOST", str(ctx.exception))
        self.assertIn("TARGET_HOST", str(ctx.exception.hint))

    def test_refuse_source_locale_et_cible_distante(self):
        with self.assertRaises(ConfigError) as ctx:
            support.load_config(SOURCE_HOST="", TARGET_HOST="h")
        self.assertIn("SOURCE_HOST", str(ctx.exception))

    def test_le_refus_de_topologie_precede_le_controle_de_porte(self):
        """L'ordre des controles n'est pas indifferent.

        Le controle de l'inventaire ou du coffre n'a de sens que si les
        deux cotes sont distants. Annoncer « mot de passe de coffre trop
        permissif » d'une configuration mixte, dont le probleme est la
        topologie, ferait corriger un reglage qui n'est pas en cause --
        et l'exploitant repasserait, au meme resultat, sur une
        configuration ou le probleme de topologie reste entier.
        """
        with tempfile.TemporaryDirectory() as tmp:
            coffre = Path(tmp) / "vault-pass"
            coffre.write_text("secret\n")
            coffre.chmod(0o644)
            with self.assertRaises(ConfigError) as ctx:
                support.load_config(
                    SOURCE_HOST="h", TARGET_HOST="",
                    OSD_VAULT_PASSWORD_FILE=str(coffre),
                )
            self.assertIn("TARGET_HOST est vide", str(ctx.exception))

    def test_accepte_deux_cotes_locaux(self):
        cfg = support.load_config(SOURCE_HOST="", TARGET_HOST="")
        self.assertEqual(cfg.get("SOURCE_HOST"), "")

    def test_accepte_deux_cotes_distants(self):
        cfg = support.load_config(SOURCE_HOST="a", TARGET_HOST="b")
        self.assertEqual(cfg.get("TARGET_HOST"), "b")

    def test_refuse_une_cle_ssh_sans_hote(self):
        """Une cle sans hote designe est une configuration incomplete.

        L'execution locale n'emploie pas SSH : pretendre le contraire
        ferait croire a une authentification par cle qui n'aura pas lieu.
        """
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "id_rsa"
            key.write_text("x")
            key.chmod(0o600)
            with self.assertRaises(ConfigError):
                support.load_config(SSH_KEY=str(key))


class TestFichierExemple(unittest.TestCase):
    """Le fichier d'exemple livre doit etre chargeable tel quel.

    C'est le premier contact de l'exploitant avec l'outil : s'il est
    invalide, l'installation echoue avant meme d'avoir commence.
    """

    def test_parse_sans_erreur(self):
        path = Path(__file__).resolve().parent.parent.parent / "config" / "config.example.conf"
        texte = path.read_text(encoding="utf-8")
        raw = cfg_mod.parse_conf(texte, origin=str(path))
        # Toute cle documentee existe dans le schema...
        self.assertEqual([k for k in raw if k not in cfg_mod.SCHEMA], [])
        # ...et toute cle du schema est documentee. Les seules tolerees
        # absentes sont celles que le gabarit ecrit en commentaire : les
        # deux obsoletes d'origine, et les six cles SSH retirees. Les
        # garder actives donnerait au recopieur une configuration qui
        # echoue a la lecture, ce qui est le pire des deux mondes.
        tolerees = {"STAGING_DIR", "REMOTE_TRANSFER",
                    "SOURCE_OS_AUTH", "TARGET_OS_AUTH"} | set(cfg_mod._OBSOLETE_SSH_KEYS)
        manquantes = set(cfg_mod.schema_keys()) - set(raw)
        self.assertLessEqual(manquantes, tolerees)
        # Chacune doit quand meme etre nommee dans le gabarit : une
        # option retiree sans mention laisse un exploitant sans piste.
        for cle in cfg_mod._OBSOLETE_SSH_KEYS:
            self.assertIn(cle, texte)

    def test_le_fichier_exemple_n_est_pas_le_mot_de_passe(self):
        path = Path(__file__).resolve().parent.parent.parent / "config" / "config.example.conf"
        self.assertIn("PASSWORD=", path.read_text(encoding="utf-8"))
        # La ligne doit etre vide : un mot de passe d'exemple dans un
        # fichier versionne est un mot de passe reel pour quiconque le
        # recopie.
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("PASSWORD="):
                self.assertEqual(line.strip(), "PASSWORD=")


class TestSchema(unittest.TestCase):
    def test_toute_cle_du_schema_a_un_defaut_declare(self):
        """Une cle sans defaut ne peut pas etre distinguee d'une cle absente.

        Le chargement ne saurait alors pas choisir, et le comportement
        dependentait de l'ordre de superposition — donc de l'historique
        d'un run.
        """
        sans_defaut = [
            key for key, spec in cfg_mod.SCHEMA.items()
            if spec.default is None
        ]
        self.assertEqual(sans_defaut, [])

    def test_les_enums_sont_en_majuscules(self):
        """Convention unique : `_coerce` met en majuscules, les choix aussi.

        Un enum dont le defaut est en minuscules et les choix en
        majuscules — ou l'inverse — echoue a la construction du defaut,
        avec un message qui ne parle que d'une valeur invalide.
        """
        for key, spec in cfg_mod.SCHEMA.items():
            if spec.kind != "enum":
                continue
            with self.subTest(cle=key):
                self.assertTrue(
                    all(c == c.upper() for c in spec.choices),
                    f"{key}: choix {spec.choices} non homogenes",
                )
                self.assertIn(str(spec.default).upper(), [c.upper() for c in spec.choices])


if __name__ == "__main__":
    unittest.main()
