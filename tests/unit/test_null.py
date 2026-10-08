"""Tests du mode simulation (`NullRunner`).

Le dry-run est la promesse la plus facile a tenir et la plus facile a
trahir. Tenir, c'est ne rien changer. Trahir, c'est declarer « rien n'a
ete modifie » apres avoir ecrase une table. Entre les deux, il n'existe
aucun etat intermediaire : ou le dump est intact, ou il ne l'est plus et
le rapport ment.

Le mode retenu n'est donc pas « le meme code avec des `if dry_run` »,
mais le **remplacement du runner** : les lectures sont deleguees au
runner reel, les mutations sont retenues. Un `if` reparti dans dix
fichiers donnerait dix endroits ou l'oubli est possible ; un runner
substitue donne un seul point de decision, et un defaut qui ne peut pas
etre oublie parce qu'il n'existe pas.

Trois proprietes doivent donc etre verifiees ici, et chacune par
**execution** — jamais par comparaison de chaine, qui ne prouverait que
la presence d'un mot :

1. une lecture deleguee atteint reellement le runner reel (sans quoi le
   dry-run ne pourrait ni valider la connexion, ni dire quoi que ce soit
   d'utile) ;
2. une mutation retenue **n'atteint jamais** le runner reel, y compris
   par les chemins qui contournent `run_script` ;
3. ce qui a ete retenue est **lisible**, sinon l'apercu ne sert a rien a
   la revue avant production.

Le defaut de `mutating` merite un test a part entiere : il vaut `False`,
donc **lecture**, donc deleguee. C'est un choix contre-intuitif — le
defaut « sur » d'un parametre nomme `mutating` aurait ete plus naturel —
et sa justification est donnee dans le test qui le fixe.
"""

from __future__ import annotations

import unittest
from typing import Any, List, Optional, Tuple

import support  # noqa: F401

from osd.adapters.null import NullRunner, _arguments, _unquote
from osd.adapters.transfer import TransferBackend
from osd.runner import Result, build_script


class RunnerTemoin:
    """Runner reel simule, qui **note** tout ce qu'il recoit.

    Il ne leve pas d'`AssertionError` sur une mutation : un test qui
    verrait l'exception ne saurait pas dire si la mutation a ete tentee
    puis corrigee, ou jamais tentee. Il enregistre, et c'est l'assertion
    du test qui tranche.
    """

    kind = "local"

    def __init__(self, *, present=("expdp", "impdp", "sqlplus")) -> None:
        self.present = set(present)
        self.calls: List[Tuple[bool, str]] = []
        self.probe_dir = "/donnees/export"
        self.ssh_opts: List[str] = []

    @property
    def label(self) -> str:
        return "temoin:local"

    def has_binary(self, name: str) -> bool:
        return name in self.present

    def allows_mutation(self) -> bool:
        return True

    def run_script(
        self, script: str, *, timeout: Optional[int] = None, mutating: bool = False
    ) -> Result:
        self.calls.append((mutating, script))
        return Result(
            rc=0,
            kv={"OSD_RC": "0", "OSD_ECHO": "lu"},
            rows=["LU"],
            stderr="",
            duration_s=0.01,
            command="temoin",
        )

    def mutations(self) -> List[str]:
        """Appels **declares** comme mutations.

        La classification vient de l'appelant, pas du corps du script : ce
        que le temoin peut dire, c'est ce qu'on lui a affirme. C'est
        exactement ce que le `NullRunner` remplace, et donc ce qu'il faut
        observer pour verifier qu'une retenue a eu lieu.
        """
        return [script for mutating, script in self.calls if mutating]

    def lectures(self) -> List[str]:
        return [script for mutating, script in self.calls if not mutating]


#: Corps de script sans effet. Le `NullRunner` ne l'execute jamais : ce
#: qui compte pour ces tests, c'est que le script porte les lignes
#: `osd_argN` que `_arguments` relit. Utiliser un vrai corps de `shell/`
#: n'ajouterait rien et couplerait ces tests a un script dont la
#: signature evolue pour une raison etrangere au dry-run.
CORPS_NEUTRE = "# script neutre : jamais execute par le NullRunner\n"


def lecture(action: str = "size", *args: str) -> str:
    """Un script qui fait **lecture** (aucun effet de bord)."""
    return build_script(CORPS_NEUTRE, [action, *args])


def mutation(action: str, *args: str) -> str:
    """Un script qui **modifie** l'hote."""
    return build_script(CORPS_NEUTRE, [action, *args])


class TestDelegationDesLectures(unittest.TestCase):
    def test_une_lecture_atteint_le_runner_reel(self):
        """Sans lecture reelle, un dry-run ne prouve rien.

        C'est toute la valeur du mode : « la connexion aboutit-elle ? le
        schema existe-t-il ? manque-t-il de la place ? » sont exactement
        les echecs qu'on veut attraper avant d'engager un export de
        plusieurs heures. Un dry-run qui n'executerait rien ne pourrait
        repondre a aucune de ces trois questions, et son rapport serait
        vide de la seule information qui compte.
        """
        temoin = RunnerTemoin()
        null = NullRunner(temoin, "source-dryrun")

        resultat = null.run_script(lecture(), mutating=False)

        self.assertEqual(len(temoin.lectures()), 1)
        self.assertEqual(resultat.get("OSD_ECHO"), "lu")
        self.assertEqual(resultat.rows, ["LU"])
        self.assertEqual(temoin.mutations(), [])

    def test_les_lectures_ne_sont_pas_comptees_comme_mutations(self):
        temoin = RunnerTemoin()
        null = NullRunner(temoin)
        null.run_script(lecture(), mutating=False)
        self.assertEqual(null.calls, [])
        self.assertEqual(null.scripts, [])

    def test_le_delegue_repond_sur_les_binaires(self):
        """`has_binary` doit rester une lecture, sans quoi le dry-run
        ne pourrait pas signaler un `expdp` absent.

        Le delegate reel est interroge : l'information vient de l'hote,
        pas d'une hypothese locale. C'est ce qui distingue « le dry-run
        est complet » de « le dry-run suppose que tout va bien ».
        """
        self.assertTrue(NullRunner(RunnerTemoin()).has_binary("expdp"))
        self.assertFalse(NullRunner(RunnerTemoin(present=())).has_binary("expdp"))

    def test_le_repertoire_de_sonde_vient_du_delegue(self):
        """Le chemin du temoin est une propriete de l'hote.

        Le poser en dur dans le `NullRunner` ferait que la sonde
        s'executerait ailleurs que la production, et un acces refuse la
        bas serait un faux negatif.
        """
        self.assertEqual(
            NullRunner(RunnerTemoin()).probe_dir, "/donnees/export"
        )

    def test_le_libelle_annonce_la_simulation(self):
        """Un rapport qui ne distingue pas le simule du reel est trompeur.

        Le libelle apparait dans les journaux et dans les controles du
        rapport. `simule:` est le seul endroit ou l'information est
        portee, puisque le code de retour est 0 dans les deux cas.
        """
        self.assertTrue(NullRunner(RunnerTemoin()).label.startswith("simule:"))


class TestRetenueDesMutations(unittest.TestCase):
    def test_une_mutation_n_atteint_pas_le_runner_reel(self):
        temoin = RunnerTemoin()
        null = NullRunner(temoin)

        null.run_script(mutation("write", "/d", "f"), mutating=True)

        self.assertEqual(temoin.mutations(), [], "le runner reel a recu une mutation")
        self.assertEqual(len(null.calls), 1)

    def test_le_defaut_du_parametre_delegue(self):
        """`mutating` absent vaut **lecture**, donc delegue.

        Le choix est contre-intuitif et deserves une justification, car
        l'inverse (`mutating=True` par defaut) parait plus prudent.

        Avec `mutating=True` par defaut, un appel de **lecture** qui
        oublie le parametre serait retenu : la lecture rendrait un resultat
        vide, le controle qui s'appuie dessus passerait sur une donnee
        absente, et le rapport annoncerait une verification qui n'a pas
        eu lieu. L'erreur serait **silencieuse et trompeuse**.

        Avec `mutating=False` par defaut, un appel de mutation qui oublie
        le parametre s'execute reellement. L'erreur est grave, mais
        **visible** : le dump change, et l'etat le montre. Entre une
        faute silencieuse et une faute visible, la seconde est
        preferable — et le present fichier existe pour que la premiere
        n'ait pas lieu.

        Le test fixe donc le defaut, et verifie en consequence qu'une
        mutation sans parametre **atteint** le delegate.
        """
        temoin = RunnerTemoin()
        null = NullRunner(temoin)

        null.run_script(mutation("write", "/d", "f"))

        # Le temoin a bien recu l'appel : c'est la faute **visible** dont
        # le test justifie le choix du defaut. Il est classe comme lecture
        # parce que c'est ce que l'appelant a affirme, et c'est
        # précisément le defaut que ce test fixe.
        self.assertEqual(len(temoin.calls), 1)
        self.assertEqual(null.calls, [], "la mutation a ete retenue : ce n'est pas le defaut")

    def test_les_chemins_hors_run_script_sont_bloques(self):
        """Le transfert ne passe pas par `run_script`.

        `scp`, `rsync` et `sftp` sont lances par le **serveur de saut**,
        donc par `subprocess` et non par le runner de l'hote distant :
        aucune substitution de runner ne les atteint. Le seul point de
        controle possible est `allows_mutation`, et c'est exactement ce
        que `TransferBackend` interroge.

        Le test appelle donc la methode reellement utilisee, avec deux
        `NullRunner` : si la retenue ne se fait pas la, un dry-run
        deplacerait les donnees qu'il pretait ne pas toucher.
        """
        source = NullRunner(RunnerTemoin(), "source-dryrun")
        cible = NullRunner(RunnerTemoin(), "cible-dryrun")
        backend = TransferBackend(
            source_runner=source, target_runner=cible, mode="scp"
        )

        transfere = backend._run_backend(
            "scp", "/source", "/cible", "f.dmp", probe=False
        )

        self.assertFalse(transfere)
        self.assertEqual(source.delegate.mutations(), [])
        self.assertEqual(cible.delegate.mutations(), [])

    def test_la_reponse_signale_la_simulation(self):
        """`OSD_DRYRUN=1` est ce qui distingue « rien constate » de « rien fait ».

        Le `rc` vaut 0 dans les deux cas, par construction. Sans ce
        marqueur, l'absence d'erreur serait indiscernable d'un succes
        reel, et l'import simulerait avoir cree le schema cible.
        """
        temoin = RunnerTemoin()
        resultat = NullRunner(temoin).run_script(
            mutation("write", "/d", "f"), mutating=True
        )
        self.assertEqual(resultat.rc, 0)
        self.assertEqual(resultat.get("OSD_DRYRUN"), "1")
        self.assertEqual(resultat.rows, [])
        self.assertEqual(resultat.stderr, "")

    def test_le_delegue_n_a_recu_que_des_lectures(self):
        """Compte global : la promesse tient pour tout le run.

        Les tests precedents verifient un appel a la fois. Celui-ci
        verifie l'invariant, quel que soit l'ordre : apres N mutations
        retenues et M lectures deleguees, le delegate n'a recu que les M.
        """
        temoin = RunnerTemoin()
        null = NullRunner(temoin)
        for i in range(3):
            null.run_script(lecture("size", "/d", f"f{i}"), mutating=False)
            null.run_script(mutation("write", "/d", f"g{i}"), mutating=True)

        self.assertEqual(len(temoin.lectures()), 3)
        self.assertEqual(temoin.mutations(), [])
        self.assertEqual(len(null.calls), 3)
        self.assertEqual(len(null.scripts), 3)


class TestApercuDesMutations(unittest.TestCase):
    """Ce que le dry-run montre de ce qu'il a retenu.

    Une retenue invisible n'a pas d'interet operationnel : l'exploitant
    ne peut ni la relire, ni la transmettre a une revue.
    """

    def setUp(self) -> None:
        self.null = NullRunner(RunnerTemoin(), "source-dryrun")

    def test_le_resume_ne_contient_que_les_mutations(self):
        self.null.run_script(lecture("size", "/d", "f"), mutating=False)
        self.null.run_script(mutation("remove", "/d", "g"), mutating=True)
        resume = self.null.summary()
        self.assertEqual(len(resume), 1)
        self.assertIn("remove", resume[0])

    def test_les_arguments_sont_lisibles_sans_quotes(self):
        """Les simples quotes sont du mecanisme de transport.

        Elles protagent l'argv du shell, et n'apportent rien a celui qui
        lit le rapport. Les retirer est ce qui rend l'apercu comparable a
        la ligne de commande qu'il decrit.
        """
        self.null.run_script(mutation("write", "/d", "mon fichier.dmp"), mutating=True)
        self.assertEqual(self.null.summary(), ["write /d mon fichier.dmp"])

    def test_une_apostrophe_dans_un_argument_survaut(self):
        """L'apostrophe est le cas qui casse les scripts de correction.

        Un nom de fichier ou une chaine SQL qui en contient une doit
        ressortir intacte de l'apercu : c'est aussi le test le plus
        direct du mecanisme de citation, des deux cotes.
        """
        self.null.run_script(mutation("write", "/d", "o'brien.sql"), mutating=True)
        self.assertEqual(self.null.summary(), ["write /d o'brien.sql"])

    def test_le_script_complet_est_retenu_pour_revue(self):
        """Le resume ne suffit pas : le SQL doit etre relisible.

        Le resume porte les arguments ; le script complet porte les
        options Data Pump et le SQL, sans lesquels une revue ne peut pas
        se faire. Il est retenu pour partir en piece jointe du rapport.
        """
        self.null.run_script(mutation("write", "/d", "f"), mutating=True)
        self.assertEqual(len(self.null.scripts), 1)
        # Le script retenu est le script **complet** : corps, prelude et
        # arguments. C'est ce qui permet de relire les options Data Pump
        # sans rejouer le run.
        self.assertIn("write", self.null.scripts[0])
        self.assertIn("osd_arg1=", self.null.scripts[0])

    def test_l_ordre_des_mutations_est_conserve(self):
        """L'ordre est une information de revue.

        L'ecrasement d'une table apres sa creation se diagnostique
        differemment du meme ecrasement avant. L'ordre des appels est
        donc parte du rapport, pas reconstruit.
        """
        for action in ("write", "verify", "remove"):
            self.null.run_script(mutation(action, "/d", "f"), mutating=True)
        resume = self.null.summary()
        self.assertEqual([r.split()[0] for r in resume], ["write", "verify", "remove"])


class TestExtractionDesArguments(unittest.TestCase):
    """`_arguments` et `_unquote`, isoles.

    Ils sont testes separement parce que leur panne serait silencieuse :
    un resume vide n'est pas une erreur, seulement un rapport en moins.
    """

    def test_aucun_argument_donne_un_resume_vide(self):
        self.assertEqual(_arguments("echo rien\n"), "")

    def test_seules_les_lignes_osd_arg_sont_lues(self):
        script = "osd_arg1=write\nOSD_PATH=/d\nosd_arg2=/d\nautre=1\n"
        self.assertEqual(_arguments(script), "write /d")

    def test_une_valeur_vide_est_conservee(self):
        """Un argument vide est une information, pas une absence.

        `osd_arg2=''` dans la commande reelle designe un repertoire vide
        — le repertoire courant. Le distinguer d'un argument non fourni
        changerait le sens de l'apercu.
        """
        self.assertEqual(_arguments("osd_arg1=''\nosd_arg2=/d\n"), " /d")

    def test_un_guillemet_simple_interne_est_restaure(self):
        self.assertEqual(_unquote("'o'\\''brien'"), "o'brien")

    def test_une_chaine_non_citee_est_rendue_telle_quelle(self):
        self.assertEqual(_unquote("nu"), "nu")

    def test_les_simples_quotes_poses_autour_sont_retirees(self):
        self.assertEqual(_unquote("'nu'"), "nu")

    def test_une_valeur_multi_lignes_est_restituee_entiere(self):
        """Un DDL tient sur plusieurs lignes : c'est la quote qui borde,
        pas la ligne.

        Couper a la premiere ligne perd non pas seulement du texte, mais
        la fermeture de la quote — et avec elle la possibilite pour la
        redaction de reconnaitre ce qu'elle doit masquer.
        """
        script = build_script(
            "true", ["/ as sysdba", "grant A to X;\ngrant B to Y;\n"]
        )
        resume = _arguments(script)
        self.assertIn("grant A to X;", resume)
        self.assertIn("grant B to Y;", resume)

    def test_une_valeur_non_refermee_n_est_pas_restituee_brute(self):
        """Une quote ouverte n'est pas un argument, c'est un secret a
        risque.

        La valeur non refermable est precisement celle dont on ne sait
        pas ou elle s'arrete — celle qui peut contenir l'empreinte du
        mot de passe. La restituer ferait porter au rapport le contenu
        que sa redaction devait proteger, en un endroit ou personne ne
        la re-verifie.
        """
        self.assertEqual(_arguments("osd_arg1='tronc\n"), "[argument non lisible]")


class TestSubstitutionEffectiveDansLeTransfert(unittest.TestCase):
    def test_une_sonde_reelle_ne_tourne_pas_en_simulation(self):
        """La sonde elle-meme est une mutation, donc elle est retenue.

        Une sonde « reussie » en dry-run dirait que `scp` fonctionne sur
        l'hote, alors qu'aucune copie n'a eu lieu. Le refus de la sonde
        doit donc se voir : c'est ce qui distingue « je ne sais pas »
        de « ca marche ».
        """
        source = NullRunner(RunnerTemoin(), "source-dryrun")
        cible = NullRunner(RunnerTemoin(), "cible-dryrun")
        backend = TransferBackend(
            source_runner=source, target_runner=cible, mode="scp"
        )
        ok, raison = backend._probe("scp", "/source", "/cible")
        self.assertFalse(ok)
        # Le motif doit nommer la simulation, pas un refus de droits :
        # « copie refusee » enverrait l'exploitant verifier des
        # permissions sur un transfert que personne n'a tente.
        self.assertIn("simule", raison)
        self.assertEqual(source.delegate.mutations(), [])
        self.assertEqual(cible.delegate.mutations(), [])


class TestSubstitutionEffectiveDuDdl(unittest.TestCase):
    """Le DDL passe par le runner, qui decide — jamais le pipeline.

    C'est la propriete 2 du module, appliquee au seul chemin d'ecriture
    du projet : `OracleAdapter.execute`. Le flag `mutating` y est
    **declare**, pas demande a l'appelant, parce qu'il n'existe aucun
    appel legitime qui ecrirait sans ecrire. Un oubli ici vaudrait :
    le dry-run delegue au runner reel, qui applique le DDL — et le
    rapport annonce « rien n'a ete change » sur un compte cree.
    """

    def _adaptateur(self, temoin):
        from osd.adapters.oracle import OracleAdapter, OracleSide

        return OracleAdapter(
            OracleSide(
                name="cible", connect="CIBLE", schema="HR",
                directory="DP_DIR", wallet="", user="", password="",
                sysdba=True, runner=temoin,
            )
        )

    def test_le_ddl_est_retenu_par_le_nullrunner(self):
        reel = RunnerTemoin()
        null = NullRunner(reel, "dry-run")
        oracle = self._adaptateur(null)

        oracle.execute("create user OSDCREE identified by values 'S:X'")

        # Le runner reel n'a rien recu : la substitution est effective,
        # et pas seulement annoncee.
        self.assertEqual(reel.calls, [])
        self.assertEqual(reel.mutations(), [])
        # Ce qui a ete retenu est le DDL lui-meme, et il est lisible :
        # c'est lui que l'exploitant relit avant d'autoriser un run.
        self.assertEqual(len(null.scripts), 1, null.scripts)
        self.assertIn("create user OSDCREE", null.scripts[0])
        self.assertIn("create user OSDCREE", " ".join(null.summary()))

    def test_une_requete_reste_deleguee(self):
        """Le drapeau ne doit pas rendre toute la base muette en simulation.

        Sans les lectures, le dry-run ne pourrait ni valider la
        connexion ni mesurer l'espace — il ne verifierait rien, et
        dirait « tout a ete verifie ».
        """
        reel = RunnerTemoin()
        oracle = self._adaptateur(NullRunner(reel, "dry-run"))

        oracle.query("select 1 from dual")

        self.assertTrue(reel.lectures(), "la lecture n'a pas ete deleguee")
        self.assertEqual(reel.mutations(), [])

    def test_le_resume_ne_laisse_passer_aucune_empreinte(self):
        """Le DDL reste revoyable, l'empreinte non — et la difference
        n'est pas de degre.

        La chaine complete est exercee : le DDL part au runner, la
        valeur est extraite du script, puis redigee. C'est le point ou
        une extraction mal bornee laisserait une quote ouverte, et ou la
        redaction, qui ne reconnaitrait plus une valeur fermee, ne
        masquerait rien. Le rapport est une piece conservee, et
        l'empreinte se craque hors ligne.
        """
        from osd.redact import redact

        reel = RunnerTemoin()
        null = NullRunner(reel, "dry-run")
        oracle = self._adaptateur(null)

        oracle.execute(
            "create user OSDCREE identified by values 'S:DEADBEEF;T:CAFEBABE'"
            " default tablespace USERS;\n"
            "grant CREATE SESSION to OSDCREE;\n"
        )

        resume = redact(" ".join(null.summary()))
        self.assertIn("create user OSDCREE", resume)
        self.assertIn("default tablespace USERS", resume)
        self.assertIn("grant CREATE SESSION", resume)
        self.assertNotIn("DEADBEEF", resume)
        self.assertNotIn("CAFEBABE", resume)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
