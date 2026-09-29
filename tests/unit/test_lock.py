"""Tests du verrou d'exclusion mutuelle.

La duplication concurrente est le risque le plus severe du projet, et le
plus difficile a voir : deux `osd` qui exportent le meme schema vers la
meme cible ne Leverage aucun des deux une erreur apparente. Le second
import ecrase ce que le premier vient d'ecrire, les deux rapports
disent « succes », et le constat n'apparait qu'a la verification des
donnees, des semaines plus tard.

Le verrou est donc teste sur ses trois proprietes : il **bloque** le
deuxieme processus, il **liberе** meme apres une exception (les signaux
SIGINT/SIGTERM lèvent), et il ne confond pas « occupe » avec « residue
d'un processus mort » — un verrou perime bloquerait definitivement
l'ordonnanceur, ce qui est un autre genre de panne.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from support import SRC_DIR

from osd import exit_codes as ec
from osd.errors import OsdError
from osd.lock import Lock, acquire, lock_key


class _FauxCfg:
    """Configuration minimale pour les tests de `lock_key`.

    `lock_key` n'appelle que `.get()`. Lui faire subir `validate()`
    obligerait a n'employer que des noms de schemas valides, et le
    test perdrait precisement les cas qu'il doit couvrir : un nom de
    fichier doit rester sur meme quand l'entree ne l'est pas.
    """

    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class TestCleDeVerrou(unittest.TestCase):
    def test_stable_pour_le_meme_couple(self):
        a = lock_key(support.load_config())
        b = lock_key(support.load_config())
        self.assertEqual(a, b)

    def test_differente_pour_un_couple_different(self):
        """Deux couples distincts doivent pouvoir tourner en parallele.

        Un verrou trop grossier (par exemple unique par serveur) ferait
        dependre de l'ordre de passage entre deux duplications sans
        rapport, ce qui, en ordonnanceur, se traduit par un echec
        ephemere et inexplique.
        """
        base = support.load_config()
        autre = support.load_config(TARGET_SCHEMA="AUTRE")
        self.assertNotEqual(lock_key(base), lock_key(autre))

    def test_le_nom_de_fichier_est_sur(self):
        """Le nom entre dans un chemin et dans un `mkdir`.

        Un `/` ou une espace casserait le repli `mkdir`, **silencieusement** :
        le `mkdir` echouerait sur un chemin invalide, le verrou serait
        juge libre, et la concurrence — que ce repli existe justement
        pour empecher — passerait.

        Les schemas employes ici sont volontairement **invalides** comme
        identifiants Oracle. `lock_key` n'a pas a valider : son contrat
        est de produire un nom sur a partir de n'importe quelle chaine,
        parce qu'il s'appuie sur `DBA_USERS` et sur des noms arbitraires
        qu'il ne controle pas.
        """
        for schema in ("A", "S", "AVANT_APRES", "schema avec espace",
                       "a/b", "../evasion", "1", "", "x" * 200):
            with self.subTest(schema=schema):
                cle = lock_key(_FauxCfg({"SOURCE_SCHEMA": "HR",
                                         "TARGET_SCHEMA": schema}))
                self.assertNotIn("/", cle)
                self.assertNotIn(" ", cle)
                self.assertNotIn("..", cle)
                self.assertNotIn(cle, ("", ".", ".."))
                # Le nom doit rester lisible : c'est lui que l'on
                # reconnait en listant le repertoire.
                self.assertLessEqual(len(cle), 64)

    def test_le_prefixe_lisible_dite_le_couple(self):
        """Le prefixe permet de reconnaitre un verrou sans l'ouvrir.

        Sans lui, l'operateur qui liste le repertoire de verrous ne voit
        qu'une suite de condensats SHA et ne peut pas savoir quel
        couple de schemas bloque — il ne peut donc pas decider s'il
        doit attendre ou lever le verrou.
        """
        cle = lock_key(support.load_config(SOURCE_SCHEMA="HR", TARGET_SCHEMA="UAT"))
        self.assertIn("HR", cle)
        self.assertIn("UAT", cle)


class TestExclusionMutuelle(unittest.TestCase):
    """L'exclusion est testee entre **deux processus**, pas en dinandins.

    Les verrous `fcntl` appartiennent au processus : un second
    `lockf` pris depuis le meme processus reussit et remplace le
    precedent. Un test « je prends le verrou deux fois » passerait donc
    toujours, y compris si la protection etait completement absente.
    Les enfants ci-dessous sont de vrais processus, obtenus par
    `subprocess`, car c'est la seule maniere d'eprouver ce que
    l'ordonnanceur和其它 outils declenchent reellement.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "verrou"

    def _essai_dans_un_enfant(self, path: Path, description: str):
        """Demande a un processus separe de prendre le verrou.

        Le retour distingue trois issues, et non deux : `OK` (pris),
        `REFUSE` (une `OsdError` a ete levee, ce qui est le refus
        attendu) et `CRASH` (une autre exception, donc un defaut de
        l'outil — que `assertRaises` ne verrait pas, puisqu'il classe
        tous les echecs ensemble).
        """
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
            "from pathlib import Path\n"
            "from osd.lock import Lock\n"
            "try:\n"
            "    with Lock(Path(sys.argv[1]), description=sys.argv[2]):\n"
            "        print('OK')\n"
            "except Exception as exc:\n"
            "    from osd.errors import OsdError\n"
            "    if not isinstance(exc, OsdError):\n"
            "        print('CRASH:' + type(exc).__name__)\n"
            "    else:\n"
            "        print('REFUSE ' + str(exc.code) + ' ' + exc.message.replace('\\n', ' '))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, str(path), description],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        return proc.stdout.decode().strip(), proc.stderr.decode()

    def test_un_second_processus_est_refuse(self):
        """Le cas reel : deux `osd` sur le meme couple source/cible."""
        with Lock(self.path, description="premier"):
            sortie, err = self._essai_dans_un_enfant(self.path, "second")
            self.assertTrue(sortie.startswith("REFUSE 2 "), f"{sortie!r} / {err}")

    def test_le_refus_porte_le_code_prerequis(self):
        """Le code 2 et non 0 : c'est ce que l'ordonnanceur regarde."""
        with Lock(self.path, description="premier"):
            sortie, _ = self._essai_dans_un_enfant(self.path, "second")
            self.assertTrue(sortie.startswith("REFUSE 2 "), sortie)

    def test_apres_liberation_le_verrou_est_reprenable(self):
        with Lock(self.path, description="premier"):
            pass
        sortie, err = self._essai_dans_un_enfant(self.path, "suivant")
        self.assertEqual(sortie, "OK", f"stderr: {err}")

    def test_le_verrou_se_libere_apres_une_exception(self):
        """Le `with` doit liberer meme en cas d'echec metier.

        C'est le chemin des signaux : SIGINT leve une exception, et si
        le verrou n'etait pas libere, l'execution suivante — celle de
        la reprise, celle-la meme qui vient d'echouer — se refuserait
        l'entree. Le verrou transformerait un echec recuperable en
        blocage definitif.
        """
        with self.assertRaises(RuntimeError):
            with Lock(self.path):
                raise RuntimeError("echec metier")
        sortie, err = self._essai_dans_un_enfant(self.path, "reprise")
        self.assertEqual(sortie, "OK", f"stderr: {err}")

    def test_libere_a_la_sortie_du_bloc_dans_tous_les_cas(self):
        for leve in (KeyboardInterrupt, SystemExit, RuntimeError):
            with self.subTest(exception=leve.__name__):
                with self.assertRaises(leve):
                    with Lock(self.path):
                        raise leve()
                with Lock(self.path):
                    pass

    def test_reacquirable_apres_liberation(self):
        for _ in range(5):
            with Lock(self.path) as v:
                self.assertIsNotNone(v)

    def test_un_verrou_d_une_autre_cle_ne_bloque_pas(self):
        """Deux couples distincts doivent pouvoir tourner en parallele.

        Un verrou trop grossier ferait dependre de l'ordre de passage
        entre deux duplications sans rapport : en ordonnanceur, cela se
        traduit par un echec ephemere et inexplique.
        """
        autre = self.path.parent / "autre"
        with Lock(self.path, description="couple A"):
            sortie, err = self._essai_dans_un_enfant(autre, "couple B")
        self.assertEqual(sortie, "OK", f"stderr: {err}")

    def test_le_refus_designe_le_titulaire(self):
        """Un operateur doit pouvoir savoir **qui** bloque.

        Sans cette information, la seule reaction possible est de
        supprimer le fichier de verrou — c'est-a-dire de lever
        exactement la concurrence qu'il fallait empecher. Le message
        doit donc nommer le titulaire, et pas seulement constater
        qu'il y en a un.
        """
        with Lock(self.path, description="osd run ABC123 HR vers UAT"):
            sortie, err = self._essai_dans_un_enfant(self.path, "second")
        self.assertTrue(sortie.startswith("REFUSE"), err)
        # Le message doit permettre de reconnaitre le run en cours sans
        # ouvrir le fichier de verrou : c'est lui qui renvoie au rapport.
        self.assertIn("ABC123", sortie)
        self.assertIn("HR vers UAT", sortie)
        # Et l'anciennete, pour distinguer un export long d'un proces
        # oublie derriere un `kill -9`.
        self.assertIn("depuis", sortie)


class TestAcquire(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_acquire_construit_le_chemin(self):
        cfg = support.load_config(LOCK_DIR=str(Path(self.tmp.name) / "verrous"))
        with acquire(cfg, description="osd run essai"):
            self.assertTrue((Path(self.tmp.name) / "verrous").is_dir())

    def test_lock_dir_vide_retombe_sur_work_dir(self):
        """`LOCK_DIR=` signifie « a cote de l'etat », pas « nulle part ».

        Un verrou ecrit dans `/tmp` partagé entre deux serveurs de saut
        se genererait lui-meme : c'est exactement la concurrence qu'il
        faut empecher, evitee par defaut.
        """
        work = Path(self.tmp.name) / "work"
        cfg = support.load_config(WORK_DIR=str(work), LOCK_DIR="")
        with acquire(cfg, description="essai"):
            entrees = list(work.iterdir())
            self.assertTrue(entrees, "aucun verrou dans WORK_DIR")

    def test_le_repertoire_de_verrou_est_en_0700(self):
        """Le repertoire contient le PID et la description du titulaire.

        Ce n'est pas un secret, mais le lister revele quels couples
        de schemas sont exploites et quand — une carte de
        l'infrastructure, lisible par tous les comptes du serveur de
        saut.
        """
        base = Path(self.tmp.name) / "verrous"
        cfg = support.load_config(LOCK_DIR=str(base))
        with acquire(cfg):
            self.assertEqual(base.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
