"""Le protocole distant, verifie par **execution**.

Les autres suites verifient les scripts distants statiquement : `sh -n`,
un catalogue de constructions non POSIX, des chaines de caracteres. C'est
necessaire — un script qui ne parse pas ne partira jamais — et ce n'est
pas suffisant. Une erreur de redirection ne se voit pas dans la syntaxe :
le script est parfaitement valide, il s'execute, et il ecrit ailleurs
que la ou l'appelant attend.

C'est exactement ce qui est arrive. `remote_listdir.sh` emettait ses
lignes avec un `printf` nu, alors que le prelude a redirige la sortie
standard vers la sortie d'erreur et conserve le canal machine sur le
descripteur 3. Le bloc `OSD_ROWS_BEGIN` / `OSD_ROWS_END` revenait donc
**vide**, la liste des parties du dump etait invisible, et l'echec se
lisait « aucun fichier de dump produit par l'export » — sur un export
qui venait de reussir, avec le fichier present sur le disque. Tous les
tests passaient : le faux runner rend des lignes qu'il invente, il
n'execute rien.

Ce module execute donc reellement chaque script, sur des fichiers
fictifs, et verifie ce que l'appelant **voit** : les cles, les lignes,
et le code de retour. Rien n'est verifie par inspection de la source.

Trois consequences de methode, a retenir pour les ajouts :

* un script nouveau s'ajoute ici avec un cas qui l'exerce, sans quoi il
  n'est verifie que par sa syntaxe ;
* une assertion porte sur le `Result` rendu par `LocalRunner`, jamais
  sur le texte du script. Un test qui chercherait `>&3` dans la source
  echouerait des qu'un script trouverait un autre moyen d'ecrire
  correctement, et passerait sur un script qui ecrit correctement par
  accident ;
* aucun cas n'exige Oracle ni le reseau. `sqlplus` et `expdp` sont
  absents de la machine de test, et c'est un fait utile : il oblige a
  n'exercer que des chemins dont le resultat est deterministe, et
  rappelle que la suite doit passer sur un poste de developpement.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SRC_DIR, isoler_home

from osd.runner import LocalRunner, build_script, load_body

SHELL_DIR = SRC_DIR.parent / "shell"

#: Les deux seules situations ou `sqlplus` et `expdp` ne sont pas
#: requis : le binaire absent du `PATH`, et l'argument manquant. Le
#: reste des cas de ces scripts suppose un client Oracle, donc une
#: instance — ce que la suite ne peut pas exiger.


class CasDeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.racine = Path(self.tmp.name)
        self.repertoire = self.racine / "dpdump"
        self.repertoire.mkdir()
        # Les scripts sourcent le profil de connexion, pour retrouver le
        # client Oracle du compte d'exploitation. Sans cette isolation,
        # un `~/.profile` developpeur qui prepend `ORACLE_HOME/bin` au
        # `PATH` ferait passer « client absent » au vert en executant le
        # vrai client. Voir `support.isoler_home`.
        isoler_home(self)

    def executer(self, nom: str, *args: str, timeout: int = 120, **kw):
        """Assemble puis execute un script distant, pour de vrai."""
        script = build_script(load_body(nom), [str(a) for a in args], **kw)
        return LocalRunner().run_script(script, timeout=timeout)

    def sans_outil_oracle(self):
        """Masque `sqlplus` et `expdp` en vidant le `PATH` herite.

        `PATH` est protegee par `build_script` — et c'est le bon
        comportement, un nom de variable venant de l'execution ne doit
        pas pouvoir redefinir celle du prelude — mais `LocalRunner`
        transmet `PATH` au processus execute. Le vider ici rend donc le
        « binaire absent », et donc le code 127, reproductible sur
        n'importe quelle machine, cliente Oracle ou non.
        """
        vide = self.racine / "bin-vide"
        vide.mkdir()
        return mock.patch.dict(os.environ, {"PATH": str(vide)})

    def ecrire(self, nom: str, taille: int = 16) -> Path:
        chemin = self.repertoire / nom
        with open(chemin, "wb") as f:
            f.write(b"x" * taille)
        return chemin

    def ecrire_parties(self) -> None:
        for i in (1, 2, 29):
            self.ecrire(f"osd_R1-{i}.dmp", taille=1024 * i)
        # Des fichiers sans rapport : ils ne doivent jamais etre confondus
        # avec une partie du dump du run courant.
        self.ecrire("probe.dmp")
        self.ecrire("osd_autre-1.dmp")
        self.ecrire("osd_R1-note.txt")


# --------------------------------------------------------------------------
# Le canal machine
# --------------------------------------------------------------------------

class TestCanalMachine(CasDeTest):
    def test_chaque_script_rend_un_bloc_de_resultat_complet(self):
        """Le bloc doit etre complet, y compris quand le script echoue.

        L'analyseur du cote Python leve une exception sur un bloc
        incomplet, et le message accuse le script d'hote — la cause
        reelle etant peut-etre une sortie sans saut de ligne final, ou
        un shell distant qui n'a pas joue le trap. Le controle est donc
        fait ici, sur du reel, plutot que laisse au premier run reel.
        """
        self.ecrire("sonte.txt")
        cas = [
            ("remote_space.sh", [self.repertoire]),
            ("remote_listdir.sh", [self.repertoire, "osd_", ".dmp"]),
            ("remote_pathinfo.sh", ["verify", self.repertoire, "sonte.txt"]),
            ("remote_which.sh", ["sh"]),
            ("remote_exec.sh", ["true"]),
        ]
        for nom, args in cas:
            with self.subTest(script=nom):
                r = self.executer(nom, *args)
                # Leve une `RemoteProtocolError` si l'un des deux
                # marqueurs manque : l'assertion qui suit est donc
                # documentee par l'absence d'exception.
                self.assertIn("OSD_RESULT_BEGIN", r.stdout_raw, nom)
                self.assertIn("OSD_RESULT_END", r.stdout_raw, nom)
                self.assertEqual(r.rc, 0, f"{nom}: {r.kv} {r.stderr}")

    def test_un_bloc_reste_complet_quand_le_corps_sort_en_erreur(self):
        """Le trap du prelude joue meme apres un `osd_die` en pleine page.

        C'est la garantie qui permet a l'appelant de lire un message
        d'erreur de l'hote au lieu de voir un « bloc incomplet », qui
        ne dit rien du tout du probleme.
        """
        r = self.executer("remote_which.sh", "binaire_absent")
        self.assertEqual(r.rc, 127)
        self.assertIn("OSD_RESULT_END", r.stdout_raw)
        self.assertIn("binaire_absent", r.kv.get("__fatal__", ""))

    def test_la_sortie_standard_ne_contient_que_du_protocole(self):
        """Aucune ligne libre sur le canal machine.

        Le contrat tient la reponse de l'hote entierement analysee. Une
        ligne de texte libre — la sortie d'un `find`, le message d'un
        `ls` mal capture — se glisse entre deux marqueurs et
        polluerait les donnees. Elle doit donc etre sur stderr, ou
        dans le bloc de lignes.
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_R1-", ".dmp")
        self.assertEqual(r.rc, 0, r.stderr)

        marqueurs = ("OSD_RESULT_BEGIN", "OSD_RESULT_END",
                     "OSD_ROWS_BEGIN", "OSD_ROWS_END")
        dans_lignes = False
        for ligne in r.stdout_raw.splitlines():
            if ligne in marqueurs:
                dans_lignes = ligne == "OSD_ROWS_BEGIN"
                continue
            if dans_lignes or not ligne:
                continue
            self.assertIn(
                "=", ligne,
                f"ligne libre sur le canal machine : {ligne!r}",
            )

    def test_les_donnees_ne_se_perdent_pas_dans_le_journal(self):
        """Symetrique du precedent : rien de structure ne part a la derive.

        Le controle porte sur la presence effective des lignes, la ou
        l'erreur observee etait leur **absence** cote appelant alors
        qu'elles se retrouvaient dans le journal.
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_R1-", ".dmp")
        self.assertTrue(r.rows, "aucune partie listee")

    def test_une_cle_multiligne_est_signalee_et_non_tronquee(self):
        """Une valeur multi-lignes est refusee, pas amputee.

        Une cle coupee en deux donnerait deux entrees faussees, dont
        une muette, et l'appelant croirait a une absence de donnee.

        Le controle porte sur `osd_kv`, fonction du **prelude** partagee
        par tous les scripts : aucun script actuel ne lui passe de
        valeur multi-lignes, mais le garde-fou doit rester verifie pour
        le prochain qui le fera. C'est aussi pourquoi le corps employe
        ici est minimal plutot qu'un script existant — le prependu est ce
        qui est teste, pas le script qui l'appelle.
        """
        corps = "\n".join([
            "osd_kv OSD_UNE_SEULE_LIGNE 'valeur'",
            "osd_kv OSD_DEUX_LIGNES \"a$(printf '\\nb')\"",
            "exit 0",
        ])
        r = LocalRunner().run_script(build_script(corps, ["inconnu"]), timeout=60)
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.get("OSD_UNE_SEULE_LIGNE"), "valeur")
        self.assertIn("__kv_error__", r.kv)
        # La valeur refusee ne doit laisser aucune trace exploitable.
        self.assertNotIn("OSD_DEUX_LIGNES", r.kv)
        self.assertNotIn("b", r.kv)

    def test_un_code_fatal_pour_sshpass_est_devie_sans_toucher_au_bloc(self):
        """Le code processus n'est ni 5 ni 255 ; le bloc porte le vrai.

        Sous Ansible, le script sort par `sshpass`, qui renvoie son code
        propre quand il reussit — et le plugin connection/ssh lit alors
        5 comme « mot de passe incorrect » : la tache est declaree
        injoignable, la sortie complete est jetee sans un mot et sans
        retry, et l'echec se conclut sur un diagnostic muet sans rapport
        avec la cause. `impdp` sort 5 quand le job aboutit avec des
        erreurs : c'est le cas normal d'un import
        `TABLE_EXISTS_ACTION=SKIP` sur un schema deja peuple, observe
        sur une vraie 19c — l'import tournait et aboutissait, et le run
        se concluait sur « hote injoignable ». 255, lui, y signifie
        « la connexion ssh a echoue », donc reconnexions inutiles puis
        echec.

        Le code **processus** n'est donc que du transport : il doit
        eviter ces deux valeurs. La verite machine reste dans
        `OSD_RESULT_END rc=`, substituee au code processus par
        `_parse_result` — c'est elle qui doit rendre 5, pas 71. Le cas
        7 verrouille que rien d'autre n'est devie.
        """
        from osd.runner import _clean_env, _parse_result

        for brut, transporte in ((5, 71), (255, 72), (7, 7)):
            with self.subTest(code=brut):
                script = build_script(f"osd_exit {brut}\n", ["x"], env={})
                proc = subprocess.run(
                    ["/bin/sh", "-s"],
                    input=script.encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    env=_clean_env(),
                )
                resultat = _parse_result(
                    proc.stdout.decode("utf-8", "replace"),
                    proc.stderr.decode("utf-8", "replace"),
                    proc.returncode,
                    command=f"osd_exit {brut}",
                )
                self.assertEqual(
                    proc.returncode, transporte,
                    "le code processus doit eviter 5 et 255 sous Ansible",
                )
                self.assertEqual(
                    resultat.rc, brut,
                    "le bloc doit porter le code reel, quelle que soit la "
                    "valeur transportee",
                )

# --------------------------------------------------------------------------
# Arguments incomplets
# --------------------------------------------------------------------------

class TestArgumentsIncomplets(CasDeTest):
    """Un argument manquant est une erreur d'invocation, pas un incident.

    Sous le `set -u` du prelude, un `${3}` absent arrete le script sur
    « unbound variable » : le code de retour est alors 1, le meme qu'une
    erreur interne du shell, et `OSD_FATAL` est vide. L'appelant ne peut
    ni distinguer les deux, ni dire a l'exploitant quoi corriger. Le code
    64, lui, signifie « invocation incorrecte » et porte le message.
    """

    def test_un_argument_manquant_donne_64_et_un_message(self):
        cas = [
            ("remote_listdir.sh", [self.repertoire, "osd_"]),
            ("remote_pathinfo.sh", ["verify", str(self.repertoire)]),
            ("remote_sqlplus.sh", ["/@CIBLE"]),
            ("remote_datapump.sh", ["expdp"]),
        ]
        for nom, args in cas:
            with self.subTest(script=nom):
                r = self.executer(nom, *args)
                self.assertEqual(r.rc, 64, f"{nom}: {r.kv} {r.stderr}")
                fatal = r.kv.get("__fatal__", "")
                self.assertTrue(fatal, f"{nom}: aucun message, code {r.rc}")
                self.assertIn(nom.split(".")[0], fatal)
                self.assertIn("OSD_RESULT_END", r.stdout_raw, nom)

    def test_un_prefixe_vide_est_refuse(self):
        """Sans cette garde, un prefixe vide=listerait tous les `.dmp`.

        Le repertoire d'un DIRECTORY peut contenir les dumps d'autres
        runs : les attribuer au run courant les ferait transferer, puis
        supprimer au nettoyage.
        """
        r = self.executer("remote_listdir.sh", self.repertoire, "", ".dmp")
        self.assertEqual(r.rc, 64)
        self.assertEqual(r.rows, [])


# --------------------------------------------------------------------------
# remote_listdir
# --------------------------------------------------------------------------

class TestEnumeration(CasDeTest):
    def test_les_parties_du_dump_sont_listees(self):
        """Le motif est ancre sur prefixe **et** suffixe.

        Un motif large echouerait dans les deux sens : il ramasserait
        les fichiers d'un autre run, et l'attribution des parties serait
        fausse. Le prefixe porte l'identifiant du run, donc deux runs
        simultanes ne se contaminent pas.
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_R1-", ".dmp")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(sorted(r.rows),
                         ["osd_R1-1.dmp", "osd_R1-2.dmp", "osd_R1-29.dmp"])

    def test_le_nom_rend_ne_contient_pas_le_chemin(self):
        """Un chemin complet se propagerait a travers le protocole.

        Le nom doit etre reutilisable tel quel par l'appelant, qui
        construit lui-meme le chemin complet, et surtout il ne faut pas
        transporter un prefixe de repertoire variable.
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_R1-", ".dmp")
        for ligne in r.rows:
            self.assertNotIn("/", ligne)
            self.assertEqual(ligne, os.path.basename(ligne))

    def test_un_prefixe_sans_correspondance_rend_rc_zero_et_aucune_ligne(self):
        """L'absence de partie n'est pas une erreur du script.

        C'est l'appelant qui sait ce qu'un dump absent signifie ; le
        script se contente de repondre. C'est ce qui permet a
        l'etape 11 de dire « aucun fichier produit par l'export »,
        avec le bon code, au lieu de « script interrompu ».
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_R9-", ".dmp")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.rows, [])

    def test_un_repertoire_inexistant_est_refuse(self):
        r = self.executer("remote_listdir.sh", self.racine / "absent", "osd_", ".dmp")
        self.assertNotEqual(r.rc, 0)
        self.assertIn("repertoire inexistant", r.kv.get("__fatal__", ""))

    def test_un_repertoire_vide_est_accepte(self):
        """Le cas d'un DIRECTORY legitimement vide.

        Il ne doit pas etre confondu avec un repertoire absent : les
        deux ont des significations opposees pour l'operateur.
        """
        vide = self.racine / "vide"
        vide.mkdir()
        r = self.executer("remote_listdir.sh", vide, "osd_", ".dmp")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.rows, [])

    def test_un_sous_repertoire_du_meme_nom_est_ignore(self):
        """L'enumeration ne doit pas descendre dans l'arborescence.

        Un DIRECTORY peut contenir des sous-repertoires, et un fichier
        portant le nom d'une partie peut y loger. Il ne fait pas partie
        du dump : l'attribuer au run courant le ferait transferer, puis
        supprimer au nettoyage. C'est ce que faisait `find`, qui
        descend par nature.
        """
        sous = self.repertoire / "ancien"
        sous.mkdir()
        (sous / "osd_R1-7.dmp").write_text("x", encoding="utf-8")
        self.ecrire("osd_R1-1.dmp")
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_R1-", ".dmp")
        self.assertEqual(r.rows, ["osd_R1-1.dmp"])

    def test_un_prefixe_contenant_un_glob_ne_decouvre_rien(self):
        """Le prefixe est litteral, le suffixe ne l'est pas.

        Un prefixe cite reste litteral : un caractere de glob qu'il
        contiendrait decouvrirait des fichiers sans rapport, et le motif
        du run en perdrait son ancrage.
        """
        self.ecrire("abc.dmp")
        self.ecrire("n'importe_quoi.dmp")
        r = self.executer("remote_listdir.sh", self.repertoire, "a*", ".dmp")
        self.assertEqual(r.rows, [])

    def test_un_nom_avec_espace_est_renvoye_tel_quel(self):
        """Un nom contient des espaces : le protocole doit les preserver.

        Un `for` sans `IFS`, ou un `read` sans quoting, casserait le nom
        en deux et l'appelant chercherait un fichier qui n'existe pas.
        """
        self.ecrire("osd_R1-1 2.dmp")
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_R1-", ".dmp")
        self.assertEqual(r.rows, ["osd_R1-1 2.dmp"])

    def test_l_enumeration_ne_mentionne_aucun_fichier_etranger(self):
        """Seuls les fichiers du run courant sont listes.

        Un fichier sans rapport se glisse dans l'inventaire soit par un
        motif trop large, soit par une descente dans l'arborescence ;
        dans les deux cas il serait transfere, puis supprime au
        nettoyage.
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_R1-", ".dmp")
        self.assertNotIn("probe.dmp", r.rows)
        self.assertNotIn("osd_autre-1.dmp", r.rows)
        self.assertNotIn("osd_R1-note.txt", r.rows)


class TestSuppression(CasDeTest):
    def test_les_fixtures_sont_supprimes_et_comptees(self):
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_", ".dmp",
                          "unlink", "osd_R1-1.dmp", "osd_R1-2.dmp")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.get_int("OSD_REMOVED", -1), 2)
        self.assertFalse((self.repertoire / "osd_R1-1.dmp").exists())
        self.assertTrue((self.repertoire / "osd_R1-29.dmp").exists(),
                        "un fichier non demande a ete supprime")

    def test_un_nom_absent_ne_compte_pas(self):
        """Compter une suppression qui n'a pas eu lieu induirait en erreur.

        Le rapport doit dire ce qui a reellement disparu : c'est ce
        nombre qui permet de conclure que le nettoyage a ete complet.
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_", ".dmp",
                          "unlink", "osd_R1-1.dmp", "osd_R1-999.dmp")
        self.assertEqual(r.get_int("OSD_REMOVED", -1), 1)

    def test_un_nom_avec_un_separateur_est_ignore(self):
        """`../` viserait un fichier hors du repertoire demande.

        Les noms viennent d'une enumeration, donc ils sont deja
        controles ; le garde-fou ne protege pas l'outil, il protege
        l'operateur d'un `clean` lance sur un repertoire inattendu.
        """
        cible = self.racine / "precieux.txt"
        cible.write_text("ne pas toucher", encoding="utf-8")
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_", ".dmp",
                          "unlink", "../precieux.txt")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertTrue(cible.exists(), "un fichier hors repertoire a ete supprime")
        self.assertEqual(r.get_int("OSD_REMOVED", -1), 0)

    def test_une_action_inconnue_est_refusee(self):
        r = self.executer("remote_listdir.sh", self.repertoire, "osd_", ".dmp",
                          "detruire")
        self.assertNotEqual(r.rc, 0)
        self.assertIn("action inconnue", r.kv.get("__fatal__", ""))

    def test_un_motif_vide_est_admis_pour_unlink(self):
        """Le motif n'a pas de sens en suppression, donc ne l'est pas exige.

        L'usage documente est `'' '' unlink <noms...>` : la suppression
        recoit des noms deja connus, et le motif ne sert qu'a la
        reclamation, ou il est indispensable. L'exiger malgre tout a
        condamne le nettoyage — l'etape 18 rendait « 0 artefact(s)
        supprime(s) », sans erreur ni trace, alors que le dump et les
        journaux etaient la.

        C'est le symetrique exact de `test_un_prefixe_vide_est_refuse`,
        qui refuse le meme motif vide pour `list`. Les deux doivent
        coexister : refuser partout casserait le nettoyage, accepter
        partout attribuerait au run les fichiers d'un autre run.
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "", "",
                          "unlink", "osd_R1-1.dmp", "osd_R1-2.dmp")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.get_int("OSD_REMOVED", -1), 2)
        self.assertFalse((self.repertoire / "osd_R1-1.dmp").exists())
        self.assertTrue((self.repertoire / "osd_R1-29.dmp").exists())

    def test_une_action_inconnue_avec_un_motif_vide_ne_liste_rien(self):
        """Une action refusee ne doit pas se comporter comme un `list`.

        Sans le controle, `detruire` avec un motif vide tomberait dans
        le cas par defaut et inventorierait tout le repertoire : le
        repertoire d'un DIRECTORY contient les dumps des autres runs,
        et le resultat se lirait comme la liste de ses propres parties.
        """
        self.ecrire_parties()
        r = self.executer("remote_listdir.sh", self.repertoire, "", "", "detruire")
        self.assertNotEqual(r.rc, 0)
        self.assertIn("action inconnue", r.kv.get("__fatal__", ""))
        self.assertEqual(r.rows, [])


# --------------------------------------------------------------------------
# remote_space / remote_pathinfo / remote_which / remote_exec
# --------------------------------------------------------------------------

class TestEspace(CasDeTest):
    def test_un_repertoire_rend_un_nombre_doctets(self):
        r = self.executer("remote_space.sh", self.repertoire)
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertGreater(r.get_int("OSD_AVAIL_BYTES", 0), 0)
        self.assertEqual(r.get_int("OSD_EXISTS", 0), 1)

    def test_un_chemin_absent_est_signale_et_refuse(self):
        r = self.executer("remote_space.sh", self.racine / "absent")
        self.assertNotEqual(r.rc, 0)
        self.assertEqual(r.get_int("OSD_EXISTS", -1), 0)
        self.assertIn("chemin absent", r.kv.get("__fatal__", ""))

    def test_un_fichier_est_mesure_comme_un_fichier(self):
        """`df` sur un fichier donne l'espace du systeme de fichiers.

        Le nommer permet a l'appelant de rapporter une taille de
        DIRECTORY, et non l'espace libre du disque, sans le recalculer.
        """
        chemin = self.ecrire("un.dmp", 10)
        r = self.executer("remote_space.sh", chemin)
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertGreater(r.get_int("OSD_AVAIL_BYTES", 0), 0)

    def test_une_taille_verifiable(self):
        """Le nombre doit etre plausible, pas seulement present.

        L'etape 9 compare l'espace disponible a l'estimation du
        schema : une unite fausse — du KiB lu comme des octets —
        passerait le controle de non-nullite et ferait echouer, ou pire
        reussir, une verification d'espace.
        """
        from osd.checks.preflight import human

        r = self.executer("remote_space.sh", self.repertoire)
        octets = r.get_int("OSD_AVAIL_BYTES", 0)
        self.assertLess(octets, 1 << 70, "valeur aberrante")
        self.assertTrue(human(octets))
        # Coherence interne : l'espace libre derive du meme `df` doit
        # rester inferieur a l'espace total.
        self.assertLessEqual(r.get_int("OSD_AVAIL_BYTES", 0),
                             r.get_int("OSD_TOTAL_BYTES", 0))

    def _df_factice(self, nom: str, corps: str) -> Path:
        """Installe un `df` de remplacement, plus proche dans le `PATH`.

        `PATH` est transmise par `LocalRunner` au processus execute, donc
        un binaire de meme nom place en tete redefinit le `df` reel. La
        machine de test etant sous Linux, c'est le seul moyen
        d'exercer la disposition de colonnes d'AIX.
        """
        faux_bin = self.racine / "bin-factice"
        faux_bin.mkdir(exist_ok=True)
        chemin = faux_bin / "df"
        with open(chemin, "w", encoding="ascii") as f:
            f.write("#!/bin/sh\ncat <<'FIN'\n" + corps + "\nFIN\n")
        chemin.chmod(0o755)
        # Le `PATH` herite est conserve apres le faux `df` : `awk`, `tail`,
        # `dirname` et `wc` doivent rester les vrais.
        courant = os.environ.get("PATH", "")
        return mock.patch.dict(
            os.environ, {"PATH": f"{faux_bin}{os.pathsep}{courant}"})

    def test_la_disposition_de_colonnes_d_aix_est_comprise(self):
        """AIX n'affiche pas la colonne « Used » : `Free` occupe le champ 3.

        Mesure sur AIX 7.2, la sortie de `df -k` est :

            /dev/lvdata  1779957760 1036628124  42%  45313  1% /pwcdata
                          total       free    %used  iused %iused  montage

        alors que Linux rend `Used` en 3 et `Available` en 4. Lire le champ
        4 comme un compte de blocs y donne « 42% », que la validation
        rejette : l'etape 9 degradait alors en « espace non mesurable »,
        avec un avertissement invitant a verifier l'espace — alors que la
        mesure etait parfaitement possible, et que le chiffre existe.
        """
        # 1 Go de blocs au total, 400 Mo libres.
        corps = "\n".join([
            "Filesystem    1024-blocks      Free %Used    Iused %Iused"
            " Mounted on",
            "/dev/lvdata    1048576     409600   61%     12345     1%"
            " /pwcdata",
        ])
        with self._df_factice("df_aix", corps):
            r = self.executer("remote_space.sh", self.repertoire)
        self.assertEqual(r.rc, 0, f"{r.kv} {r.stderr}")
        self.assertEqual(r.get("OSD_DF_LAYOUT"), "aix")
        # 409600 KiB, et non « 61% ».
        self.assertEqual(r.get_int("OSD_AVAIL_KB", -1), 409600)
        self.assertEqual(r.get_int("OSD_TOTAL_KB", -1), 1048576)
        # `Used` est deduit, faute de colonne : 1048576 - 409600.
        self.assertEqual(r.get_int("OSD_USED_KB", -1), 1048576 - 409600)

    def test_la_disposition_de_colonnes_de_linux_est_inchangee(self):
        """Le chemin deja couvert ne doit pas avoir bouge.

        Le test precedent introduit une detection de disposition ; celle-ci
        verifie que le cas nominal, seul cas reellement rencontre en
        developpement, produit toujours les memes chiffres qu'avant.
        """
        corps = "\n".join([
            "Filesystem     1024-blocks     Used Available Capacity"
            " Mounted on",
            "/dev/sda1       1048576   204800    819776      20% /",
        ])
        with self._df_factice("df_posix", corps):
            r = self.executer("remote_space.sh", self.repertoire)
        self.assertEqual(r.rc, 0, f"{r.kv} {r.stderr}")
        self.assertEqual(r.get("OSD_DF_LAYOUT"), "posix")
        self.assertEqual(r.get_int("OSD_AVAIL_KB", -1), 819776)
        self.assertEqual(r.get_int("OSD_TOTAL_KB", -1), 1048576)
        self.assertEqual(r.get_int("OSD_USED_KB", -1), 204800)


class TestInfoChemin(CasDeTest):
    def test_un_fichier_present_est_reconnu(self):
        self.ecrire("donnees.txt")
        r = self.executer("remote_pathinfo.sh", "verify",
                          self.repertoire, "donnees.txt")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.get_int("OSD_PRESENT", 0), 1)

    def test_un_fichier_absent_est_signale(self):
        r = self.executer("remote_pathinfo.sh", "verify",
                          self.repertoire, "absent.txt")
        self.assertNotEqual(r.rc, 0)
        self.assertEqual(r.get_int("OSD_PRESENT", -1), 0)
        self.assertIn("absent apres transfert", r.kv.get("__fatal__", ""))

    def test_un_contenu_identique_est_verifie(self):
        """La verification porte sur les octets, pas sur la presence.

        Une copie tronquee doit etre detectee ; c'est tout l'objet de la
        sonde de capacite, qui valide le transfert avant de lancer un
        import de plusieurs heures.
        """
        contenu = "osd-probe\n" * 4
        (self.repertoire / "temoin.txt").write_text(contenu, encoding="ascii")
        r = self.executer("remote_pathinfo.sh", "verify",
                          self.repertoire, "temoin.txt",
                          bootstrap=f"osd_probecontent='{contenu}'")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.get_int("OSD_INTACT", -1), 1)

    def test_un_contenu_altere_est_detecte(self):
        """Le symetrique est indispensable : une sonde qui reussit
        toujours ne prouve rien et laisse croire a un transfert sain."""
        contenu = "osd-probe\n" * 4
        (self.repertoire / "temoin.txt").write_text(contenu[:20], encoding="ascii")
        r = self.executer("remote_pathinfo.sh", "verify",
                          self.repertoire, "temoin.txt",
                          bootstrap=f"osd_probecontent='{contenu}'")
        self.assertNotEqual(r.rc, 0)
        self.assertEqual(r.get_int("OSD_INTACT", -1), 0)
        self.assertIn("altere", r.kv.get("__fatal__", ""))

    def test_la_taille_est_exacte(self):
        self.ecrire("donnees.dmp", 1234)
        r = self.executer("remote_pathinfo.sh", "size",
                          self.repertoire, "donnees.dmp")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.get_int("OSD_SIZE", -1), 1234)

    def test_un_nom_avec_un_separateur_est_refuse(self):
        """Le nom vient de l'etape 13, mais un nom hostile reste refuse."""
        r = self.executer("remote_pathinfo.sh", "verify",
                          self.repertoire, "../x")
        self.assertNotEqual(r.rc, 0)
        self.assertIn("nom de fichier invalide", r.kv.get("__fatal__", ""))

    def test_une_operation_inconnue_est_refusee(self):
        self.ecrire("donnees.txt")
        r = self.executer("remote_pathinfo.sh", "detruire",
                          self.repertoire, "donnees.txt")
        self.assertNotEqual(r.rc, 0)

    def test_des_arguments_incomplets_sont_refuses(self):
        r = self.executer("remote_pathinfo.sh", "verify", str(self.repertoire))
        self.assertNotEqual(r.rc, 0)
        self.assertIn("arguments incomplets", r.kv.get("__fatal__", ""))

    def test_un_repertoire_absent_est_signale_a_l_ecriture(self):
        r = self.executer("remote_pathinfo.sh", "write",
                          self.racine / "absent", "temoin.txt")
        self.assertNotEqual(r.rc, 0)
        self.assertIn("repertoire inexistant", r.kv.get("__fatal__", ""))


class TestPresenceDeBinaires(CasDeTest):
    def test_un_binaire_present_est_signale(self):
        r = self.executer("remote_which.sh", "sh")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.get_int("OSD_FOUND", 0), 1)
        self.assertTrue(r.kv.get("OSD_PATH"))

    def test_un_binaire_absent_a_un_code_propre(self):
        """Le code 127, et non 1.

        Un code 1 serait ambigu avec une erreur interne du shell, et
        l'appelant ne saurait pas s'il doit verifier le `PATH` ou
        suspecter l'hote. `__fatal__` porte en outre le nom manquant,
        que l'exploitant peut corriger.
        """
        r = self.executer("remote_which.sh", "binaire_qui_nexiste_pas")
        self.assertEqual(r.rc, 127)
        self.assertEqual(r.get_int("OSD_FOUND", -1), 0)
        self.assertIn("binaire_qui_nexiste_pas", r.kv.get("__fatal__", ""))

    def test_aucun_binaire_fourni_est_refuse(self):
        r = self.executer("remote_which.sh", "")
        self.assertNotEqual(r.rc, 0)


class TestExecutionDistante(CasDeTest):
    def test_une_sortie_est_rendue_dans_les_lignes(self):
        """Une commande qui ecrit doit revenir dans `rows`, pas a la derive.

        Meme defaut que celui trouve sur `remote_listdir`, et pour la
        meme raison : le resultat d'un outil ne se deduit pas, il se
        lit. Un test qui n'exerce que le succes d'une commande silencieuse
        ne le verrait jamais.
        """
        r = self.executer("remote_exec.sh", "printf", "premiere ligne")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertIn("premiere ligne", r.rows)

    def test_une_sortie_sans_saut_de_ligne_final_agonit_correctement(self):
        """Le cas qui a casse le premier run reel.

        `printf 'texte'` ne termine pas sa sortie par un saut de ligne.
        Le `cat` de la sortie collait alors le marqueur de fin a la
        donnee ; l'analyseur, qui reconnait le marqueur par egalite de
        ligne, ne le voyait plus et rendait la ligne melangee. Le bloc
        restait ouvert, et le nom de fichier se retrouvait dans le
        rapport.
        """
        r = self.executer("remote_exec.sh", "printf", "texte")
        self.assertEqual(r.rc, 0, r.stderr)
        self.assertEqual(r.rows, ["texte"])
        self.assertIn("OSD_ROWS_END", r.stdout_raw)

    def test_une_sortie_terminatee_ne_gagne_pas_de_ligne_vide(self):
        """Le correctif ne doit pas laisser de trace de son passage.

        Comparer le nombre d'octets a celui des sauts de ligne aurait
        ajoute une ligne vide a toute sortie deja correctement terminee
        — et l'appelant aurait vu une partie de dump nommee « vide ».
        """
        r = self.executer("remote_exec.sh", "printf", "texte\n")
        self.assertEqual(r.rows, ["texte"])

    def test_une_ligne_vide_reelle_est_conservee(self):
        """Une ligne vide qui fait partie des donnees reste une donnee.

        Le correctif doit distinguer « sortie non terminee » de
        « sortie terminee par une ligne vide », sans quoi il effacerait
        des donnees.
        """
        r = self.executer("remote_exec.sh", "sh", "-c", r"printf 'a\n\n'")
        self.assertEqual(r.rows, ["a", ""])

    def test_une_commande_absente_a_un_code_propre(self):
        r = self.executer("remote_exec.sh", "binaire_qui_nexiste_pas")
        self.assertEqual(r.rc, 127)
        self.assertEqual(r.kv.get("OSD_MISSING_CMD"), "binaire_qui_nexiste_pas")

    def test_une_commande_en_echec_rend_son_code(self):
        r = self.executer("remote_exec.sh", "sh", "-c", "exit 3")
        self.assertEqual(r.rc, 3)

    def test_les_sorties_standard_et_erreur_sont_comptees(self):
        """Le transfert raisonne sur des octets : ils doivent etre comptes.

        Une sortie d'erreur non capturee ferait conclure a une
        reussite au transfert alors que la commande avait ecrit ailleurs
        que la ou l'appelant lit.
        """
        r = self.executer("remote_exec.sh", "sh", "-c", "printf abc")
        self.assertEqual(r.get_int("OSD_STDOUT_BYTES", -1), 3)
        self.assertEqual(r.get_int("OSD_STDERR_BYTES", -1), 0)


# --------------------------------------------------------------------------
# Les scripts Oracle, sur leurs chemins qui ne demandent pas Oracle
# --------------------------------------------------------------------------

class TestRequetesOracle(CasDeTest):
    """`sqlplus` et `expdp` ne sont pas requis pour ces cas.

    Les chemins exerces sont ceux ou l'absence du client est elle-meme
    la situation : binaire absent, argument absent, parfile absent. Ce
    sont des verifications que la sonde de dependances et le pipeline
    exploitent reellement, et elles rendent la suite utilisable sur un
    poste sans Oracle.
    """

    def test_sqlplus_absent_a_un_code_propre(self):
        with self.sans_outil_oracle():
            r = self.executer("remote_sqlplus.sh", "/@CIBLE", "select 1 from dual")
        self.assertEqual(r.rc, 127)
        self.assertEqual(r.kv.get("OSD_MISSING_CMD"), "sqlplus")
        self.assertIn("OSD_RESULT_END", r.stdout_raw)

    def test_une_requete_absente_est_refusee(self):
        with self.sans_outil_oracle():
            r = self.executer("remote_sqlplus.sh", "/@CIBLE")
        self.assertEqual(r.rc, 64)
        self.assertIn("requete absente", r.kv.get("__fatal__", ""))

    def test_expdp_absent_a_un_code_propre(self):
        parfile = self.racine / "p.par"
        parfile.write_text("dumpfile=x.dmp\n", encoding="ascii")
        with self.sans_outil_oracle():
            r = self.executer("remote_datapump.sh", "expdp", parfile)
        self.assertEqual(r.rc, 127)
        self.assertEqual(r.kv.get("OSD_MISSING_CMD"), "expdp")

    def test_un_compteur_absent_ne_rend_pas_l_etat_illisible(self):
        """`DBA_DATAPUMP_JOBS` peut n'avoir que ses colonnes d'identification.

        Une colonne manquante fait echouer la requete **entiere** en
        ORA-00904, et l'etat du job devenait donc illisible — muet, en
        particulier, sur la seule voie qui distingue un client detache
        d'un export termine. Le compteur est donc lu a part, et son
        echec est absorbe ; l'etat, lui, est indispensable.

        La vue reelle d'une instance amputee est simulee en repondant une
        erreur ORA-00904 a toute requete qui mentionne `error_count`,
        exactement comme le fait le dictionnaire quand la colonne
        n'existe pas.
        """
        from osd import exit_codes as ec
        from osd.errors import OsdError

        def query(sql: str):
            if "error_count" in sql.lower():
                raise OsdError(
                    "ORA-00904: \"ERROR_COUNT\": invalid identifier", ec.PREREQ
                )
            return [["JOB", "EXPORT", "COMPLETED"]]

        oracle = mock.Mock(spec=["query"])
        oracle.query.side_effect = query
        from osd.adapters.datapump import DataPumpAdapter

        statut = DataPumpAdapter(None, oracle=oracle).job_status("JOB")
        self.assertEqual(statut["state"], "COMPLETED")
        self.assertEqual(statut["error_count"], "")

    def test_un_compteur_lu_est_rendu(self):
        """Le chemin nominal reste couvert apres le decoupage en deux."""
        from osd.adapters.datapump import DataPumpAdapter

        oracle = mock.Mock(spec=["query"])
        oracle.query.side_effect = [
            [["JOB", "EXPORT", "COMPLETED"]],
            [["7"]],
        ]
        statut = DataPumpAdapter(None, oracle=oracle).job_status("JOB")
        self.assertEqual(statut["state"], "COMPLETED")
        self.assertEqual(statut["error_count"], "7")

    def test_un_job_absent_rend_un_dictionnaire_vide(self):
        """Pas de ligne, pas d'etat : et non pas d'etat favorable.

        La distinction est ce qui permet a l'appelant de distinguer
        « je ne sais pas » de « tout va bien » ; confondre les deux
        ferait passer un export jamais constate.
        """
        from osd.adapters.datapump import DataPumpAdapter

        oracle = mock.Mock(spec=["query"])
        oracle.query.return_value = []
        self.assertEqual(DataPumpAdapter(None, oracle=oracle).job_status("JOB"), {})

    def test_un_fatal_du_script_remonte_avec_sa_cause(self):
        """Un `OSD_FATAL` doit nommer sa cause, pas un symptome collateral.

        Un fatal du script d'hote — argument manquant, client absent du
        `PATH`, fichier temporaire impossible a creer — survient **avant**
        toute interrogation d'Oracle. Il se retrouve neanmoins dans le meme
        `Result` qu'un `rc` et des lignes, et l'analyse des colonnes le
        reduisait alors a « aucune metadonnee retournee » : un message qui
        designe l'outil et invite a verifier la connexion, alors que la
        cause etait dans le script et connue.

        Le cas reel est un `osd_tmpfile` refuse par le Bourne shell d'AIX :
        le script mourait sur `0403-041 Parameter not set`, et l'etape 4
        annoncait une base injoignable.
        """
        from osd.adapters.oracle import OracleAdapter, OracleSide
        from osd.errors import PrereqError

        fatal = "fichier temporaire impossible"

        class RunnerFatal:
            kind = "remote"
            host = "hote-fictif"
            user = "oracle"
            probe_dir = "/tmp"
            label = "ansible:source/hote-fictif"

            def __init__(self) -> None:
                self.scripts: list = []

            def has_binary(self, name: str) -> bool:
                return True

            def allows_mutation(self) -> bool:
                return True

            def run_script(self, script: str, *, timeout=None,
                           mutating: bool = False):
                from osd.runner import Result

                self.scripts.append(script)
                # Le bloc est complet et `rc` vaut 0 : c'est bien ce que
                # produisait le shell d'AIX, dont le trap ne recevait pas
                # le code de sortie. Seule la cle `__fatal__` distingueait
                # l'echec.
                return Result(
                    rc=0,
                    kv={"__fatal__": fatal},
                    rows=[],
                    stderr="0403-041 Parameter not set.\n",
                    stdout_raw=(
                        "OSD_RESULT_BEGIN\n"
                        f"OSD_FATAL={fatal}\n"
                        "OSD_RESULT_END rc=0\n"
                    ),
                )

        runner = RunnerFatal()
        side = OracleSide(
            name="source", connect="SRC", schema="HR", directory="DP_DIR",
            wallet="", user="", password="", sysdba=True, runner=runner,
        )
        oracle = OracleAdapter(side)

        # Les trois entrees qui brassent le `Result` doivent nommer la
        # cause : `query` (etape 4), `query_one` et `execute`.
        for nom, appel in (
            ("query", lambda: oracle.query("select 1 from dual")),
            ("query_one", lambda: oracle.query_one("select 1 from dual")),
            ("execute", lambda: oracle.execute("create table t (a int)")),
        ):
            with self.subTest(entree=nom):
                with self.assertRaises(PrereqError) as ctx:
                    appel()
                self.assertIn(fatal, str(ctx.exception))
                # Le message doit dire que la base n'a pas ete mise en
                # cause : c'est ce qui oriente l'exploitant vers l'hote.
                self.assertIn("avant toute connexion", str(ctx.exception.hint or ""))

    def test_une_commande_qui_lit_stdin_aboutit(self):
        """Une commande qui lit stdin doit **aboutir**, pas bloquer.

        Le contrat de ces corps est que rien n'est fourni sur stdin. Herite
        via Ansible, stdin n'est ni un terminal ni un fichier clos : toute
        lecture attend indefiniment, et l'etape ne se termine jamais.

        Le cas reel est l'etape 11 : le client Data Pump accuse reception
        d'un `userid` « / as sysdba » — la seule forme acceptee sur ces
        hotes — en affichant `Password:`, consomme une ligne de stdin, et
        l'export est reste bloque plus de deux heures sur un schema de
        2 Mo. Le meme export aboutit en 66 s avec `< /dev/null`.

        Le test reproduit la condition, avec `cat` et sans nom de fichier :
        il lit vraiment stdin, et le test a un delai. Sans la redirection,
        il expire ; avec elle, la lecture rend la main sur une fin de
        fichier. Ce qui est verifie est le **retour**, pas le code : le
        point est que le script est revenu.
        """
        r = self.executer("remote_exec.sh", "cat", timeout=30)
        self.assertIn("OSD_RESULT_END", r.stdout_raw)

    def test_les_clients_oracle_lancent_leur_commande_stdin_ferme(self):
        """Meme correction pour `expdp`/`impdp` et `sqlplus`.

        Ces deux clients demandent une saisie dans deux situations
        ordinaires : le mot de passe absent d'un `userid`, et
        l'invite « Appuyez sur Entree » de fin de script. Ni le parfile ni
        le script SQL ne passent par stdin — l'un par `parfile=`, l'autre
        par `@fichier` — donc la fermeture ne peut rien supprimer
        d'utile.

        Un client Oracle n'etant pas disponible sur la machine de test,
        l'assertion porte sur la **commande rendue**, seule partie du
        chemin que ce test peut atteindre.
        """
        for nom, args in (
            ("remote_datapump.sh", ["expdp", "/par/inexistant.par"]),
            ("remote_sqlplus.sh", ["/@CIBLE", "select 1 from dual"]),
        ):
            with self.subTest(script=nom):
                rendu = build_script(load_body(nom), [str(a) for a in args])
                self.assertIn('< "/dev/null"', rendu, nom)

    def test_un_parfile_absent_est_signale(self):
        r = self.executer("remote_datapump.sh", "expdp", self.racine / "absent.par")
        self.assertEqual(r.rc, 66)
        self.assertIn("parfile absent", r.kv.get("__fatal__", ""))

    def test_un_outil_hors_liste_blanche_est_refuse(self):
        """Seuls `expdp` et `impdp` sont acceptes.

        Un nom d'outil vient de la configuration : le transmettre a un
        shell sans controle ouvrirait la porte a l'execution d'une
        commande arbitraire sur l'hote Oracle.
        """
        parfile = self.racine / "p.par"
        parfile.write_text("dumpfile=x.dmp\n", encoding="ascii")
        r = self.executer("remote_datapump.sh", "sh", parfile)
        self.assertEqual(r.rc, 64)
        self.assertIn("outil inconnu", r.kv.get("__fatal__", ""))

    def test_le_parfile_est_supprime_meme_en_cas_erreur(self):
        """Un parfile contient `userid`, donc un mot de passe.

        Il doit disparaitre que l'export reussisse, echoue, soit
        interrompu, ou que la session SSH se coupe. Le test le verifie
        sur le chemin d'echec le plus tot possible : un nom d'outil
        errone, ou l'operateur ne pense pas au secret.
        """
        parfile = self.racine / "secret.par"
        parfile.write_text("userid=system/motdepasse\n", encoding="ascii")
        r = self.executer("remote_datapump.sh", "outil_inexistant", parfile)
        self.assertNotEqual(r.rc, 0)
        self.assertFalse(parfile.exists(),
                         "le parfile a survecu a l'echec du script")


# --------------------------------------------------------------------------
# Couverture : tout script doit etre exerce
# --------------------------------------------------------------------------

class TestCouvertureDesScripts(unittest.TestCase):
    def test_tout_script_distant_est_exerce(self):
        """Aucun `remote_*.sh` ne doit echapper a l'execution.

        Un script jamais execute ici n'est verifie que par sa syntaxe,
        ce qui laisse passer exactement la classe de defaut que ce
        module a mis au jour. La liste des cas est donc close, et ce
        controle la surveille : ajouter un script sans ajouter son cas
        fait echouer la suite, ce qui est le but.
        """
        exerces = {
            "remote_datapump.sh",
            "remote_exec.sh",
            "remote_listdir.sh",
            "remote_pathinfo.sh",
            "remote_space.sh",
            "remote_sqlplus.sh",
            "remote_which.sh",
        }
        presents = {p.name for p in SHELL_DIR.glob("remote_*.sh")}
        self.assertEqual(
            presents - exerces, set(),
            "scripts distants sans cas d'execution dans ce module",
        )

    def test_chaque_script_est_valide_shell(self):
        """Controle syntaxique, sur le script **assemble**.

        Verifier le fichier seul laisserait passer un `build_script`
        dont l'assemblage serait invalide — et c'est l'assemblage qui
        part sur l'hote.
        """
        sh = _sh()
        if sh is None:  # pragma: no cover - POSIX sans sh n'est pas supporte
            self.skipTest("aucun shell disponible")
        for nom in sorted(p.name for p in SHELL_DIR.glob("remote_*.sh")):
            with self.subTest(script=nom):
                script = build_script(load_body(nom), ["a", "b", "c", "d"])
                proc = subprocess.run([sh, "-n"], input=script,
                                      text=True, capture_output=True, timeout=60)
                self.assertEqual(proc.returncode, 0,
                                 f"{nom}: {proc.stderr.strip()}")

    def test_le_prelude_est_a_son_tour_valide(self):
        """Le prelude est verifie isolement, et non par accident.

        Il est insere dans tous les scripts : une erreur y passerait
        inapercue tant qu'aucun script ne prend le chemin fautif, et
        tous les executeraient au mauvais moment — en production, sur
        l'hote Oracle, au milieu d'un export.
        """
        sh = _sh()
        if sh is None:  # pragma: no cover
            self.skipTest("aucun shell disponible")
        for nom in ("prelude.sh",):
            proc = subprocess.run([sh, "-n"],
                                  input=(SHELL_DIR / nom).read_text(encoding="utf-8"),
                                  text=True, capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr.strip())


def _sh() -> str:
    import shutil

    return shutil.which("sh") or "/bin/sh"


if __name__ == "__main__":
    unittest.main()
