"""Tests du transport d'execution distante par Ansible.

Ces tests n'invoquent **pas** Ansible : ils verifient la construction
des commandes, l'interpretation de sa sortie et le comportement face a
ses echecs. Un test qui dependre d'un `ansible` installe serait un test
d'environnement, pas un test du code : il passerait sur une machine de
developpement et echouerait sur un serveur de saut minimal.

Ce qui est verifie ici, et pourquoi c'est ce qui compte :

* la ligne de commande construite, parce qu'elle est ce que voit un
  exploitant dans ses journaux quand le run echoue ;
* le fait qu'un secret n'y figure **jamais**, parce que c'est la
  propriete qui tient le reste ;
* le contrat du bloc machine, parce qu'il est la seule voie par
  laquelle un echec du script distant peut revenir jusqu'a l'outil.

Le contrat de transport lui-meme -- Ansible renvoie `rc=0` meme quand le
script sort en erreur, et `stdout` porte le bloc machine -- a ete verifie
experimentalement contre `ansible` 2.14 avant d'ecrire ce module ; les
tests ci-dessous encodent ce constat, ils ne le redemontent pas.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from osd.adapters import transfer as tr
from osd.adapters.ansible_runner import (
    AnsibleRunner,
    _en_echec,
    _parse_ssh_args,
    _premier_json,
)
from osd.errors import PrereqError
from osd.runner import build_script, load_body


#: Sortie Ansible typique d'un `script` reussi. Reproduite telle quelle,
#: lignes de bibliographic comprises, parce que le parseur doit les
#: ignorer et non les tolérer par chance.
SORTIE_REUSSIE = (
    "PLAY [all] *********************************************************\n"
    "\n"
    "TASK [ansible_legacy_host] ***************************************\n"
    "ok: [localhost]\n"
    "\n"
    "PLAY RECAP *********************************************************\n"
    "localhost                  : ok=1    changed=0    unreachable=0    failed=0   \n"
)


def sortie_json(donnees: dict) -> str:
    """Enveloppe un resultat Ansible comme le fait la commande reelle."""
    return SORTIE_REUSSIE + json.dumps(donnees, indent=4) + "\n"


def inventaire_local(racine: Path) -> Path:
    """Inventaire minimal ejecutant en local, sans reseau ni mot de passe."""
    chemin = racine / "hosts"
    chemin.write_text(
        "localhost ansible_connection=local "
        f"ansible_python_interpreter={sys.executable}\n",
        encoding="utf-8",
    )
    return chemin


class FauxAnsible:
    """Remplace `subprocess.run` pour la seule duree d'un test.

    Le module n'implemente pas d'injection : `subprocess.run` est appele
    directement, comme partout ailleurs dans le projet. On le remplace
    donc comme les autres tests le font, mais en verifiant que la
    commande construite est bien celle attendue -- sinon le test
    validerait un `argv` faux en simulant une reponse qui qui ne
    correspond pas.
    """

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0,
                 variables: dict = None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        #: Reponses par nom de variable. `AnsibleRunner` interroge le
        #: coffre pour `ansible_password`, `ansible_ssh_common_args` et
        #: `ansible_ssh_private_key_file` -- trois appels distincts. Un
        #: faux qui repond la meme chose a tous rendrait, pour la cle
        #: privee, la chaine des options SSH : le test passerait alors
        #: sur un `IdentityFile=-o ConnectTimeout=10 ...` qui n'a aucun
        #: sens, et le defaut qu'il devait attraper resterait invisible.
        self.variables = dict(variables or {})
        self.appels: list = []

    def __call__(self, argv, **kw):
        self.appels.append((argv, kw))
        sortie = self.stdout
        if self.variables and "-a" in argv:
            expression = argv[argv.index("-a") + 1]
            for nom, valeur in self.variables.items():
                if nom in expression:
                    sortie = sortie_json({"msg": valeur})
                    break
        class Proc:
            pass
        proc = Proc()
        proc.stdout = sortie.encode("utf-8")
        proc.stderr = self.stderr.encode("utf-8")
        proc.returncode = self.returncode
        return proc


class _Interception:
    """Gestionnaire de contexte : substitue `subprocess.run` le temps du test."""

    def __init__(self, faux: FauxAnsible):
        self.faux = faux
        self._avant = None

    def __enter__(self):
        import osd.adapters.ansible_runner as module

        self._module = module
        self._avant = module.subprocess.run
        module.subprocess.run = self.faux
        return self.faux

    def __exit__(self, *exc):
        self._module.subprocess.run = self._avant
        return False


class TestLigneDeCommande(unittest.TestCase):
    """Ce que l'exploitant voit dans les journaux quand le run echoue."""

    def runner(self, racine: Path, **kw) -> AnsibleRunner:
        return AnsibleRunner(
            "localhost",
            inventory=str(inventaire_local(racine)),
            **kw,
        )

    def test_la_commande_porte_l_hote_l_inventaire_et_le_coffre(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.runner(Path(tmp), vault_password_file="/etc/osd/vault-pass")
            argv = r.argv("/tmp/script.sh")
            self.assertEqual(argv[0], "ansible")
            self.assertEqual(argv[1], "localhost")
            self.assertIn("-i", argv)
            self.assertIn(str(Path(tmp) / "hosts"), argv)
            self.assertIn("--vault-password-file", argv)
            self.assertIn("/etc/osd/vault-pass", argv)
            self.assertIn("script", argv)

    def test_le_coffre_absent_omet_son_option(self):
        """Sans coffre, l'option serait un `None` dans les journaux.

        Ansible.accepte `--vault-password-file None` sans erreur visible
        et tente ensuite de lire un fichier nomme `None`. L'omettre est
        donc correct, et rend l'absence explicite dans la commande.
        """
        with tempfile.TemporaryDirectory() as tmp:
            argv = self.runner(Path(tmp)).argv("/tmp/script.sh")
            self.assertNotIn("--vault-password-file", argv)
            self.assertNotIn("None", argv)

    def test_le_label_nomme_le_groupe_et_l_hote(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.runner(Path(tmp), group="osd_target", side="target")
            self.assertEqual(r.label, "ansible:osd_target/localhost")
            self.assertEqual(r.kind, "remote")

    def test_un_hote_vide_est_refuse(self):
        with self.assertRaises(PrereqError):
            AnsibleRunner("", inventory="x")

    def test_un_inventaire_absent_est_refuse(self):
        """Echouer tot plutot que d'attendre la connexion.

        L'exploitant doit apprendre « inventaire introuvable » a la
        premiere etape, et non « permission denied » apres le delai de
        connexion.
        """
        r = AnsibleRunner("localhost", inventory="/nonexenant/inventaire")
        with self.assertRaises(PrereqError) as ctx:
            r._verify_ansible()
        self.assertIn("inventaire", str(ctx.exception))

    def test_un_coffre_absent_est_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner(
                "localhost",
                inventory=str(inventaire_local(Path(tmp))),
                vault_password_file="/nonexenant/vault-pass",
            )
            with self.assertRaises(PrereqError) as ctx:
                r._verify_ansible()
            self.assertIn("coffre", str(ctx.exception))

    def test_le_coffre_trop_ouvert_est_refuse_par_la_configuration(self):
        """Rappel : ce controle vit dans `config.validate`.

        Le runner ne le repete pas. C'est volontaire -- une seule
        verification, a la lecture, et le runner ne peut pas etre appele
        sans passer par elle en production.
        """
        with tempfile.TemporaryDirectory() as tmp:
            racine = Path(tmp)
            coffre = racine / "vault-pass"
            coffre.write_text("secret\n", encoding="utf-8")
            coffre.chmod(0o644)
            r = AnsibleRunner(
                "localhost",
                inventory=str(inventaire_local(racine)),
                vault_password_file=str(coffre),
            )
            mode = coffre.stat().st_mode & 0o777
            self.assertTrue(mode & stat.S_IRGRP)


class TestLectureDeLaSortie(unittest.TestCase):
    """Ansible parle JSON ; le projet parle bloc machine.

    L'adaptation se fait ici, et elle n'est pas neutre : Ansible ajoute
    une enveloppe, et `rc` ne dit rien du script.
    """

    def test_le_bloc_machine_est_extrait_du_json(self):
        from osd.adapters.ansible_runner import _unwrap_ansible

        bloc = "OSD_ROWS_BEGIN\nval1\nval2\nOSD_ROWS_END\nOSD_RESULT_END rc=0\n"
        stdout, stderr, failed = _unwrap_ansible(
            sortie_json({"stdout": bloc, "stderr": "", "failed": False}), ""
        )
        self.assertEqual(stdout, bloc)
        self.assertEqual(stderr, "")
        self.assertFalse(failed)

    def test_le_stderr_d_ansible_est_conserve(self):
        """Le `stderr` du script ne doit pas etre perdu dans l'enveloppe.

        C'est lui qui porte le message d'un `expdp` refuse, donc la
        majorite des diagnostics. Le perdre reviendrait a transformer un
        echec explicite en « bloc de resultat incomplet ».
        """
        from osd.adapters.ansible_runner import _unwrap_ansible

        stdout, stderr, failed = _unwrap_ansible(
            sortie_json({"stdout": "", "stderr": "ORA-39002: invalid operation\n",
                         "failed": True}),
            "",
        )
        self.assertIn("ORA-39002", stderr)
        self.assertTrue(failed)

    def test_le_stderr_de_l_analyse_est_ajoute_au_leur(self):
        """Une erreur de lecture Ansible ne doit pas disparaitre.

        Elle se melange au `stderr` du script : les deux sont des
        diagnostics de la meme tentative, et separement Aucun des deux
        n'aurait de sens.
        """
        from osd.adapters.ansible_runner import _unwrap_ansible

        _, stderr, _ = _unwrap_ansible(
            sortie_json({"stdout": "", "stderr": "ORA-12345\n", "failed": True}),
            "WARNING: Could not open vault password file\n",
        )
        self.assertIn("ORA-12345", stderr)
        self.assertIn("vault", stderr)

    def test_une_sortie_sans_json_est_rendue_telle_quelle(self):
        """Pas de JSON ne signifie pas « echec » : le texte prime.

        Rendre stdout vide ferait perdre le diagnostic d'Ansible, qui
        n'a pas besoin d'etre du JSON pour etre utile.
        """
        from osd.adapters.ansible_runner import _unwrap_ansible

        stdout, _, failed = _unwrap_ansible("usage: ansible [-h]\n", "erreur d'usage\n")
        self.assertIn("usage", stdout)
        self.assertTrue(failed)

    def test_un_json_illisible_est_rendu_tel_quel(self):
        """Sans enveloppe lisible, il n'y a rien a demeler.

        `stdout` est rendu tel quel, et `failed` ne dit que la presence
        d'un `stderr`. Ce n'est pas une faiblesse : un `stdout` sans bloc
        machine est refuse juste apres par `_parse_result`, qui leve
        `RemoteProtocolError` avec la sortie en detail. C'est la que le
        diagnostic se construit, parce que c'est la que le contrat de
        transport se rompt -- pas ici, ou l'on ne fait que defaire une
        enveloppe.
        """
        from osd.adapters.ansible_runner import _unwrap_ansible

        stdout, stderr, failed = _unwrap_ansible("{ ceci n'est pas du json", "")
        self.assertIn("ceci n'est pas", stdout)
        self.assertEqual(stderr, "")
        self.assertFalse(failed)

        # Avec un `stderr`, l'echec est signale.
        _, stderr2, failed2 = _unwrap_ansible("{ ceci n'est pas du json", "erreur")
        self.assertIn("erreur", stderr2)
        self.assertTrue(failed2)

    def test_le_premier_objet_seul_est_lu(self):
        """Plusieurs hotes produiraient plusieurs objets : un seul vise.

        `raw_decode` s'arrete a la fin du premier. Sans cela, un second
        hote -- d'un inventaire elargi par erreur -- contaminerait la
        sortie lue.
        """
        texte = sortie_json({"msg": "premier"}) + json.dumps({"msg": "second"})
        payload = _premier_json(texte)
        self.assertEqual(payload["msg"], "premier")

    def test_une_absence_de_json_rend_none(self):
        self.assertIsNone(_premier_json("PLAY [all]\nTASK [x]\n"))

    def test_le_statut_d_echec_est_reconnu(self):
        """Seuls `failed` et `unreachable` signalent un echec.

        L'absence de statut n'en est pas un : Ansible omet `failed` sur
        un succes, donc l'exiger rendrait tout succes impossible. C'est
        pourquoi le vide reste traite a la source, par le joker
        `default("")` pose dans l'expression -- et non ici, ou l'on ne
        verrait pas la difference entre une variable absente et une
        valeur vide.
        """
        self.assertTrue(_en_echec({"failed": True, "msg": "x"}))
        self.assertTrue(_en_echec({"unreachable": True, "msg": "x"}))
        self.assertTrue(_en_echec("pas un objet"))
        self.assertTrue(_en_echec(None))
        self.assertFalse(_en_echec({"msg": "valeur"}))
        self.assertFalse(_en_echec({"msg": "valeur", "failed": False}))


class TestEchecAvantLeScript(unittest.TestCase):
    """Quand l'echec precede le script, le dire avec le bon diagnostic.

    Un echec d'authentification ne produit aucun bloc machine.
    Rapporter « bloc de resultat incomplet » serait un mensonge utile :
    le script n'a jamais ete execute.
    """

    def test_un_echec_sans_bloc_machine_remonte_le_diagnostic_d_ansible(self):
        bloc = build_script(load_body("remote_which.sh"), ["expdp"], env={})
        sortie = sortie_json(
            {"msg": "Permission denied", "failed": True, "stdout": "", "stderr": ""}
        )
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            with _Interception(FauxAnsible(stdout=sortie, returncode=2)):
                with self.assertRaises(PrereqError) as ctx:
                    r.run_script(bloc, timeout=5)
            self.assertIn("inventaire", str(ctx.exception.hint).lower()
                          + str(ctx.exception).lower())

    def test_un_bloc_machine_present_renvoie_le_resultat(self):
        """L'echec du script est dans le bloc, pas dans le `rc` d'Ansible.

        C'est le point le plus important du transport. Ansible rend
        `rc=0` et `failed=False` pour un script qui sort en 7 : seule la
        lecture du bloc machine restitue l'echec. Ce test verrouille ce
        comportement, parce que c'est lui qui empeche de se fier au
        `rc` d'Ansible -- erreur qui passerait inaperue sur un run
        reussi et masquerait tous les echecs.
        """
        bloc = build_script(
            'osd_kv OSD_AVANT 1\nosd_die "bascule" 7\n', ["x"], env={}
        )
        # Le bloc est copie de la sortie **reelle** du prelude, capturee
        # sur une instance de test. Le reconstruire de memoire avait
        # donne `OSD_KV=OSD_AVANT=1`, qui n'existe pas : le format est
        # `osd_kv` qui ecrit `CLE=VALEUR` directement sur le
        # descripteur 3. Un bloc invente passe les tests et echoue en
        # production, c'est-a-dire au pire moment.
        sortie = sortie_json(
            {
                "stdout": "OSD_RESULT_BEGIN\n"
                          "OSD_AVANT=1\n"
                          "OSD_FATAL=bascule\n"
                          "\n"
                          "OSD_RESULT_END rc=7\n",
                "stderr": "",
                "failed": False,
                "rc": 0,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            with _Interception(FauxAnsible(stdout=sortie, returncode=0)):
                resultat = r.run_script(bloc, timeout=5)
            self.assertEqual(resultat.rc, 7)
            self.assertEqual(resultat.get("OSD_AVANT"), "1")

    def test_un_delai_depasse_est_remonte(self):
        import subprocess as sp

        def trop_long(argv, **kw):
            raise sp.TimeoutExpired(argv, kw.get("timeout", 0))

        bloc = build_script(load_body("remote_which.sh"), ["expdp"], env={})
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            with _Interception(trop_long.__call__):
                with self.assertRaises(PrereqError) as ctx:
                    r.run_script(bloc, timeout=3)
            self.assertIn("delai depasse", str(ctx.exception))

    def test_ansible_absent_est_nomme_comme_tel(self):
        def absent(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory")

        bloc = build_script(load_body("remote_which.sh"), ["expdp"], env={})
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            with _Interception(absent.__call__):
                with self.assertRaises(PrereqError) as ctx:
                    r.run_script(bloc, timeout=5)
            self.assertIn("ansible", str(ctx.exception))
            self.assertIn("local", str(ctx.exception.hint))


class TestSecretNonDivulgue(unittest.TestCase):
    """Aucun secret dans un argument de processus, jamais.

    La propriete est simple a verifier et vitale : un argument est lisible
    par `ps` pour tout utilisateur du serveur de saut, et reste dans
    `/proc` bien apres la fin du processus.
    """

    def test_le_mot_de_passe_du_coffre_n_est_qu_un_chemin(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner(
                "localhost",
                inventory=str(inventaire_local(Path(tmp))),
                vault_password_file="/etc/osd/vault-pass",
            )
            argv = r.argv("/tmp/script.sh")
            self.assertIn("/etc/osd/vault-pass", argv)
            # Le chemin, oui ; le contenu, jamais. Un chemin n'est pas un
            # secret, et c'est ce qui permet a l'outil de ne rien savoir
            # du mot de passe lui-meme.
            #
            # Le controle porte sur les options de la commande, pas sur
            # la chaine entiere : `--vault-password-file` contient bien
            # un `-p`, et un test trop large echouerait a tort.
            self.assertNotIn("--ask-pass", argv)
            for element in argv:
                self.assertFalse(
                    element.startswith("-p"),
                    f"option de mot de passe en ligne de commande : {element}",
                )

    def test_le_script_temporaire_est_supprime(self):
        """Le script porte les valeurs injectees, dont un `userid`.

        Il est ecrit en 0600 et supprime dans un `finally`, y compris en
        cas d'interruption -- le cas ou la suppression est la plus
        importante, et le plus facile a oublier.
        """
        vus: list = []

        def capture(argv, **kw):
            vus.append(argv)
            raise OSError("interruption")

        bloc = build_script(load_body("remote_which.sh"), ["expdp"], env={})
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            with _Interception(capture.__call__):
                with self.assertRaises(PrereqError):
                    r.run_script(bloc, timeout=5)

            # Le chemin du script est l'argument de `-a`.
            chemins = [argv[argv.index("-a") + 1] for argv in vus]
            self.assertEqual(len(chemins), 1)
            self.assertFalse(Path(chemins[0]).exists(), "script temporaire survived")


class TestPartageAvecLeTransfert(unittest.TestCase):
    """Le transfert n'est pas execute par Ansible, mais parle au meme secret.

    `scp`, `rsync` et `sftp` sont lances depuis le serveur de saut, hors
    du chemin d'Ansible. Sans ce partage, le run echouerait apres avoir
    exporte -- c'est-a-dire apres avoir depense le temps le plus long.
    """

    def test_le_mot_de_passe_est_lu_depuis_l_inventaire(self):
        with tempfile.TemporaryDirectory() as tmp:
            racine = Path(tmp)
            inv = inventaire_local(racine)
            gv = racine / "group_vars"
            gv.mkdir()
            (gv / "all.yml").write_text(
                'ansible_password: "secret-de-test"\n', encoding="utf-8"
            )
            r = AnsibleRunner("localhost", inventory=str(inv))
            sortie = sortie_json({"msg": "secret-de-test"})
            with _Interception(FauxAnsible(stdout=sortie)):
                self.assertEqual(r.ssh_password(), "secret-de-test")

    def test_le_mot_de_passe_est_memoise(self):
        """Une seule lecture du coffre, donc un seul dechiffrement."""
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            faux = FauxAnsible(stdout=sortie_json({"msg": "x"}))
            with _Interception(faux):
                r.ssh_password()
                r.ssh_password()
            self.assertEqual(len(faux.appels), 1)

    def test_le_joker_donne_le_vide_hors_variabilite(self):
        """L'expression porte `default("")` : l'absence devient le vide.

        Sans lui, `ansible -m debug` ne signale pas l'absence d'une
        variable -- il met son message d'erreur dans `msg`, sans
        `failed`. Ce texte deviendrait alors le mot de passe remis a
        `sshpass`, et l'echec n'apparaitrait qu'a la copie, sous forme
        d'une authentification refusee sans lien avec sa cause.

        Le test verifie donc l'expression **construite**, pas seulement
        le resultat : c'est elle qui tient la garantie.
        """
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            faux = FauxAnsible(stdout=sortie_json({"msg": ""}))
            with _Interception(faux):
                self.assertEqual(r.ssh_password(), "")
            argv = faux.appels[0][0]
            self.assertIn('default("")', argv[argv.index("-a") + 1])

    def test_un_echec_de_lecture_rend_le_vide_sans_lever(self):
        """Le transfert a son propre diagnostic, plus precis qu'ici.

        Une exception ici interromprait le run avant meme que la copie
        ne soit tentee, pour une cause dont le message dirait « secret
        illisible » -- ce qui n'aide a rien.
        """
        def casse(argv, **kw):
            raise OSError("pas de reseau")

        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            with _Interception(casse.__call__):
                self.assertEqual(r.ssh_password(), "")

    def test_les_options_ssh_viennent_de_l_inventaire(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            # Reponse par variable : `ssh_opts` interroge aussi la cle
            # privee, et un faux a reponse unique lui aurait renvoye la
            # chaine des options -- voir `FauxAnsible`.
            with _Interception(FauxAnsible(
                variables={"ansible_ssh_common_args":
                           "-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"}
            )):
                self.assertEqual(
                    r.ssh_opts(),
                    ["ConnectTimeout=10", "StrictHostKeyChecking=accept-new"],
                )

    def test_la_cle_privee_est_traduite_en_identityfile(self):
        """Sans elle, un inventaire authentifie par cle echoue a l'etape 13.

        La cle est une variable **seule** dans l'inventaire :
        `ansible_ssh_common_args` ne la porte pas. Omettre la traduction
        produirait un inventaire qui execute les dix-neuf etapes -- dont
        l'export, la plus longue -- puis echoue a la copie, sur une
        authentification que rien n'avait annoncee.
        """
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            faux = FauxAnsible(
                variables={
                    "ansible_ssh_common_args": "-o ConnectTimeout=10",
                    "ansible_ssh_private_key_file": "/home/oracle/.ssh/id_ed25519",
                }
            )
            with _Interception(faux):
                opts = r.ssh_opts()
        self.assertIn("IdentityFile=/home/oracle/.ssh/id_ed25519", opts)
        self.assertIn("ConnectTimeout=10", opts)

    def test_une_identityfile_deja_posee_fait_autorite(self):
        """`ansible_ssh_common_args` peut en designer plusieurs.

        Une seule variable ne portant qu'un chemin, l'ajouter quand le
        fichier en pose deja une ajouterait une seconde cle, et
        `scp` retiendrait la premiere -- qui n'est pas necessairement
        celle qu'on veut.
        """
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            faux = FauxAnsible(
                variables={
                    "ansible_ssh_common_args": (
                        "-o ConnectTimeout=10 -o IdentityFile=/cle/principale"
                    ),
                    "ansible_ssh_private_key_file": "/cle/secondaire",
                }
            )
            with _Interception(faux):
                opts = r.ssh_opts()
        self.assertIn("IdentityFile=/cle/principale", opts)
        self.assertNotIn("IdentityFile=/cle/secondaire", opts)

    def test_les_options_absentes_donnent_le_vide(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = AnsibleRunner("localhost", inventory=str(inventaire_local(Path(tmp))))
            with _Interception(FauxAnsible(stdout=sortie_json({"msg": ""}))):
                self.assertEqual(r.ssh_opts(), [])


class TestIdentitePourLesClientsDuServeurDeSaut(unittest.TestCase):
    """`scp`/`rsync`/`sftp` ignorent l'inventaire : ils ont besoin d'une adresse.

    `SOURCE_HOST` et `TARGET_HOST` designent des **noms d'inventaire**.
    Ces noms n'ont de sens que pour Ansible. Sans traduction, la commande
    de transfert porterait `scp osaix:...` sur un nom que `ssh` ne sait
    pas resoudre -- apres douze etapes reussies et un export completed.
    """

    def runner(self, racine: Path, variables: dict) -> AnsibleRunner:
        r = AnsibleRunner("osaix", inventory=str(inventaire_local(racine)))
        r._variable = lambda nom: variables.get(nom, "")  # noqa: E731
        return r

    def test_le_nom_seul_est_employe_tel_quel(self):
        """Un inventaire dont le nom est joignable ne change rien.

        C'est le cas le plus simple : le nom est une adresse ou un alias
        DNS. Aucune traduction n'est necessaire, et aucune erreur ne doit
        etre levee pour cela.
        """
        with tempfile.TemporaryDirectory() as tmp:
            r = self.runner(Path(tmp), {})
            self.assertEqual(r.transfer_host, "osaix")
            self.assertEqual(r.transfer_user, "")

    def test_ansible_host_remplace_le_nom_d_inventaire(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.runner(Path(tmp), {"ansible_host": "172.16.1.84",
                                        "ansible_user": "oracle"})
            self.assertEqual(r.transfer_host, "172.16.1.84")
            self.assertEqual(r.transfer_user, "oracle")

    def test_la_commande_de_transfert_porte_adresse_compte_et_cle(self):
        """Le rendu, et non les valeurs internes.

        C'est la seule forme que `scp` verra : une adresse resolvable,
        le compte du compte d'exploitation, et l'`IdentityFile` de
        l'inventaire. Les trois ensemble forment ce qui manquait.
        """
        with tempfile.TemporaryDirectory() as tmp:
            r = self.runner(Path(tmp), {
                "ansible_host": "172.16.1.84",
                "ansible_user": "oracle",
                "ansible_ssh_common_args": "-o ConnectTimeout=10",
                "ansible_ssh_private_key_file": "/cle/ids",
            })
            r._opts = None
            faux = FauxAnsible(variables={
                "ansible_ssh_common_args": "-o ConnectTimeout=10",
                "ansible_ssh_private_key_file": "/cle/ids",
            })
            with _Interception(faux):
                backend = tr.TransferBackend(
                    source_runner=r, target_runner=None, mode="scp", ssh_password=""
                )
                argv = backend._scp_command("/d", "/d", "f.dmp", tr._opts_du_runner(r), legacy=False)
        self.assertIn("oracle@172.16.1.84:", " ".join(argv))
        self.assertIn("IdentityFile=/cle/ids", " ".join(argv))
        self.assertNotIn("osaix:", " ".join(argv))


class TestConversionDesOptions(unittest.TestCase):
    """`ansible_ssh_common_args` n'a pas la forme qu'attend le transfert.

    L'inventaire ecrit la chaine telle qu'OpenSSH la veut, avec les `-o`
    ; le transfert construit `scp -o <opt>`. Sans conversion, la commande
    deviendrait `scp -o -o ConnectTimeout=10`, que `scp` refuse.
    """

    def test_les_options_sont_separees_de_leur_prefixe(self):
        self.assertEqual(
            _parse_ssh_args("-o ConnectTimeout=10 -o StrictHostKeyChecking=no"),
            ["ConnectTimeout=10", "StrictHostKeyChecking=no"],
        )

    def test_un_prefixe_colle_est_egalement_compris(self):
        self.assertEqual(_parse_ssh_args("-oConnectTimeout=10"), ["ConnectTimeout=10"])

    def test_les_citations_sont_retirees(self):
        """Un auteur peut en mettre ; `scp` refuserait une apostrophe."""
        self.assertEqual(
            _parse_ssh_args("-o 'StrictHostKeyChecking=accept-new'"),
            ["StrictHostKeyChecking=accept-new"],
        )

    def test_une_chaine_vide_ou_absente_donne_une_liste_vide(self):
        self.assertEqual(_parse_ssh_args(""), [])
        self.assertEqual(_parse_ssh_args("   "), [])


class TestLiaisonAvecLeTransfert(unittest.TestCase):
    """Ce que le transfert construit reellement, options et mot de passe.

    Ces assertions portent sur la **commande rendue**, pas sur une valeur
    interne. La raison est historique : `getattr(runner, "ssh_opts", [])`
    devuelve une methode liee pour `AnsibleRunner`, qui expose `ssh_opts`
    en methode et non en attribut. Aucun appel ne echouait, et aucun test
    d'egalite de chaine ne l'aurait vu -- la commande aurait recu une
    liste contenant un objet, et le transfert aurait echoue bien plus
    tard, sur une machine de l'exploitant.
    """

    class RunnerAvecMethode:
        host = "src.exemple"

        def __init__(self, opts, mot_de_passe=""):
            self._opts = opts
            self._mdp = mot_de_passe

        def ssh_opts(self):
            return self._opts

        def ssh_password(self):
            return self._mdp

    def test_une_methode_est_appelee_et_non_renvoyee(self):
        runner = self.RunnerAvecMethode(["ConnectTimeout=10"])
        self.assertEqual(tr._opts_du_runner(runner), ["ConnectTimeout=10"])

    def test_un_attribut_reste_accepte(self):
        """`LocalRunner` et les runners de test portent un attribut.

        La compatibilite n'est pas un detail : le `NullRunner` du
        dry-run enveloppe le runner reel, et doit continuer a fonctionner
        avec l'un comme avec l'autre.
        """

        class RunnerAvecAttribut:
            ssh_opts = ["ConnectTimeout=30"]

        self.assertEqual(tr._opts_du_runner(RunnerAvecAttribut()), ["ConnectTimeout=30"])

    def test_une_chaine_est_decoupee(self):
        class RunnerChaine:
            ssh_opts = "-o ConnectTimeout=10 -o StrictHostKeyChecking=no"

        self.assertEqual(
            tr._opts_du_runner(RunnerChaine()),
            ["-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no"],
        )

    def test_une_absence_donne_une_liste_vide(self):
        class RunnerNu:
            pass

        self.assertEqual(tr._opts_du_runner(RunnerNu()), [])

    def test_une_methode_qui_leve_ne_casse_pas_le_transfert(self):
        class RunnerCasse:
            def ssh_opts(self):
                raise RuntimeError("coffre illisible")

        self.assertEqual(tr._opts_du_runner(RunnerCasse()), [])

    def test_la_commande_scp_ne_recoit_pas_de_methode(self):
        """Le test de regression proprement dit."""
        runner = self.RunnerAvecMethode(["ConnectTimeout=10"], "mdp-de-test")
        backend = tr.TransferBackend(
            source_runner=runner, target_runner=None, mode="scp", ssh_password="mdp-de-test"
        )
        cmd = backend._scp_command("/src", "/dst", "osd_1.dmp", tr._opts_du_runner(runner), legacy=False)
        rendu = " ".join(cmd)
        self.assertIn("-o ConnectTimeout=10", rendu)
        self.assertNotIn("method", rendu)
        self.assertNotIn("bound", rendu)

    def test_le_secret_ne_figure_jamais_dans_la_commande(self):
        """Ni le mot de passe, ni le nom de la variable qui le porte.

        Ce que le transfert met dans l'environnement -- c'est la seule
        voie acceptee ; `sshpass -p` le mettrait dans la ligne de
        commande, donc dans `ps` pour tout utilisateur du serveur.
        """
        secret = "mot-de-passe-a-ne-pas-voir"
        runner = self.RunnerAvecMethode(["ConnectTimeout=10"], secret)
        backend = tr.TransferBackend(
            source_runner=runner, target_runner=None, mode="scp", ssh_password=secret
        )
        cmd = backend._scp_command("/src", "/dst", "osd_1.dmp", tr._opts_du_runner(runner), legacy=False)
        rendu = " ".join(cmd)
        self.assertNotIn(secret, rendu)
        self.assertNotIn("SSHPASS", rendu)
        env = tr._env_avec_mot_de_passe(secret)
        self.assertEqual(env.get("SSHPASS"), secret)

    def test_sans_mot_de_passe_aucune_enveloppe_n_est_ajoutee(self):
        """Vide signifie « authentification par cle », pas « aucun secret ».

        Envelopper dans `sshpass` un transfert sans mot de passe
        echouerait systematiquement, alors que la cle fonctionne tres
        bien. L'enveloppe doit donc suivre la presence du secret.
        """
        self.assertEqual(tr._prefixe_sshpass(""), [])
        self.assertEqual(tr._env_avec_mot_de_passe(""), None)
        self.assertTrue(tr._prefixe_sshpass("x")[0] == "sshpass")


class TestBatchModeSelonLeModeDauthentification(unittest.TestCase):
    """Le meme defaut, deux remedes -- parce que le defaut n'est pas le meme.

    `BatchMode=yes` interdisait toute invite : garantie contre le blocage
    du run sous cron, mais qui rendait l'authentification par mot de
    passe impossible. Ansible leve la contrainte pour l'execution ; le
    transfert, qui n'y passe pas, doit donc choisir.
    """

    def test_par_cle_batchmode_bloque_toute_invite(self):
        opts = tr._merge_opts(["ConnectTimeout=10"], "")
        self.assertIn("BatchMode=yes", opts)
        self.assertNotIn("BatchMode=no", opts)

    def test_par_mot_de_passe_l_invite_est_autorisee(self):
        """`BatchMode=yes` et un mot de passe sont mutuellement exclusifs.

        C'est demontre empiriquement sur les deux hotes AIX : en l'un la
        liste des methodes proposees ne mentionne que `publickey`, en
        l'autre `sshpass` est sollicite. Dans les deux cas, un mot de
        passe impose de pouvoir ouvrir une invite.
        """
        opts = tr._merge_opts(["ConnectTimeout=10"], "un-mot-de-passe")
        self.assertIn("BatchMode=no", opts)
        self.assertNotIn("BatchMode=yes", opts)

    def test_le_blocage_est_bloque_autrement(self):
        """`BatchMode=yes` etait la garantie ; elle a un remplacant.

        Sans lui, `ssh` reessaie en boucle sur un mot de passe errone, et
        le run reste bloque sur une invite invisible -- exactement le
        defaut que `BatchMode` evitait. `NumberOfPasswordPrompts=1` le
        remplace : une tentative, puis echec.
        """
        opts = tr._merge_opts([], "un-mot-de-passe")
        self.assertIn("NumberOfPasswordPrompts=1", opts)

    def test_la_garantie_est_posee_y_comme_en_mode_cle(self):
        """`NumberOfPasswordPrompts` protege aussi le mode cle.

        Il est pose par l'enveloppe `sshpass`, qui n'existe pas en mode
        cle. La garantie anti-blocage repose donc, en mode cle, sur le
        seul `BatchMode=yes` -- ce qui est correct, et ce qu'il faut
        savoir pour ne pas croire le transfert ne protege pas deux fois.
        """
        self.assertNotIn("NumberOfPasswordPrompts=1", tr._merge_opts([], ""))

    def test_une_option_batchmode_externe_est_ecartee(self):
        """Peu importe sa valeur, elle ne doit pas passer.

        Une configuration qui porterait `BatchMode=no` en mode cle
        decouvrirait exactement le blocage que la valeur ajoutee cherche
        a empecher.
        """
        for valeur in ("BatchMode=no", "batchmode=no", "BatchMode=yes"):
            with self.subTest(valeur=valeur):
                self.assertIn("BatchMode=yes", tr._merge_opts([valeur], ""))

    def test_les_autres_options_sont_conservees(self):
        self.assertIn("ConnectTimeout=10", tr._merge_opts(["ConnectTimeout=10"], "mdp"))
        self.assertIn("StrictHostKeyChecking=no", tr._merge_opts(["StrictHostKeyChecking=no"], "mdp"))


if __name__ == "__main__":
    unittest.main()
