"""Tests de `generate_project.py`, le script de copie du projet.

Ce script produit une arborescence destinee a quelqu'un d'autre : a un
collaborateur, a un auditeur, ou a l'architecte d'une cible. Sa seule
promesse est qu'il ne livre ni artefact de run, ni configuration reelle,
ni depot git.

Cette promesse ne peut pas etre verifiee « de confiance ». Un fichier
d'ajoute au depot et oublie dans la liste de copie disparait
silencieusement de ce que l'on transmet ; un artefact laisse sur le
disque et non ignore se retrouve livre avec un dump et un journal de run
contenant des noms de schemas. Les deux sont invisibles sans verification
mecanique, d'ou ces tests.

Ils s'executent contre un **depot jetable** construit a la volee, et non
contre le depot courant : la liste des fichiers est relue depuis le
`git ls-files` de ce depot-la. On teste donc la regle « on copie ce que
git dit », et non une liste figee qui divergerait du depot sans qu'on le
voie.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

RACINE = Path(__file__).resolve().parent.parent.parent
SCRIPT = RACINE / "generate_project.py"


def _depot_jetable(base: Path) -> Path:
    """Cree un depot git minimal et retourne son repertoire.

    Le script est execute avec `-C <depot>` : il resout sa racine depuis
    le chemin du script, de sorte que le point de verification est
    l'execution reelle plutot qu'un appel de fonction.
    """
    depot = base / "depot"
    depot.mkdir()
    (depot / "src").mkdir()
    (depot / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (depot / "lanceur").write_text("#!/bin/sh\n", encoding="utf-8")
    (depot / "lanceur").chmod(0o755)
    (depot / "lu.txt").write_text("texte\n", encoding="utf-8")

    # Artefacts : versionnes ou non, ils ne doivent jamais etre livres.
    (depot / "dump.dmp").write_text("binaire\n", encoding="utf-8")
    (depot / "journal.log").write_text("journal\n", encoding="utf-8")
    (depot / "config.conf").write_text("PASSWORD=secret\n", encoding="utf-8")
    (depot / "pycache").mkdir()
    (depot / "pycache" / "module.pyc").write_bytes(b"\x00\x01")

    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    subprocess.run(["git", "init", "-q", str(depot)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(depot), "add", "src/module.py", "lanceur", "lu.txt"],
        check=True,
        env=env,
    )
    return depot


def _executer(
    depot: Path,
    cible: Path,
    *,
    par_main: bool = False,
    extra: Sequence[str] = (),
) -> subprocess.CompletedProcess:
    """Lance le script sur `depot`, avec `RACINE` forcee.

    `RACINE` est la seule constante que le script lit au chargement.
    La surcharger permet de tester le script contre un depot jetable au
    lieu du depot courant, sans avoir a dupliquer le script lui-meme.

    `par_main` passe par `main()` plutot que par `copier()`, ce qui
    exerce aussi l'analyse des arguments et les gardes qu'elle pose —
    dont le refus de l'auto-copie.
    """
    code = (
        "import importlib.util, sys, pathlib\n"
        "spec = importlib.util.spec_from_file_location('gp', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod.RACINE = pathlib.Path(sys.argv[2])\n"
        "dest = pathlib.Path(sys.argv[3])\n"
        "if sys.argv[4] == 'main':\n"
        "    sys.argv = [sys.argv[1]] + sys.argv[5:] + [str(dest)]\n"
        "    mod.main()\n"
        "else:\n"
        "    mod.copier(dest, False)\n"
    )
    return subprocess.run(
        [
            os.sys.executable,
            "-c",
            code,
            str(SCRIPT),
            str(depot),
            str(cible),
            "main" if par_main else "copier",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


class TestRegleDeCopie(unittest.TestCase):
    """Ce qui doit se retrouver dans la copie, et ce qui ne doit pas."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.depot = _depot_jetable(self.base)
        self.cible = self.base / "copie"
        self.rc = _executer(self.depot, self.cible)
        if self.rc.returncode != 0:
            self.fail(f"echec du script :\n{self.rc.stdout}\n{self.rc.stderr}")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_les_fichiers_versionnes_sont_copies(self) -> None:
        """Les fichiers listes par git sont livres."""
        for relatif in ("src/module.py", "lanceur", "lu.txt"):
            with self.subTest(relatif=relatif):
                self.assertTrue((self.cible / relatif).is_file(), relatif)

    def test_le_contenu_est_identique_a_la_source(self) -> None:
        """La copie est fidele, octet pour octet.

        Un script qui « reecrit » un fichier a sa sauce introduirait un
        ecart entre ce que l'on developpe et ce que l'on transmet, sans
        que la revue de code le remarque.
        """
        for relatif in ("src/module.py", "lanceur", "lu.txt"):
            with self.subTest(relatif=relatif):
                source = (self.depot / relatif).read_bytes()
                copie = (self.cible / relatif).read_bytes()
                self.assertEqual(copie, source, relatif)

    def test_les_repertoires_de_reception_sont_crees(self) -> None:
        """Les parents absents sont crees, sinon la copie echoue a plat."""
        self.assertTrue((self.cible / "src").is_dir())

    def test_le_bit_executable_est_preserve(self) -> None:
        """`lanceur` reste executable, `lu.txt` reste non executable.

        Un fichier arrive non executable chez celui qui le recoit est un
        echec au moment de l'execution, chez lui, sans trace ici.
        """
        with self.subTest("lanceur"):
            self.assertTrue(os.access(self.cible / "lanceur", os.X_OK))
        with self.subTest("lu.txt"):
            self.assertFalse(os.access(self.cible / "lu.txt", os.X_OK))

    def test_aucun_artefact_de_run_est_livre(self) -> None:
        """Ni dump, ni journal, ni configuration reelle.

        Ces fichiers sont presents sur le disque du developpeur et
        absents du depot. Si la copie suivait le disque, elle livrerait
        un dump et un journal nommant des schemas reels.
        """
        for nom in ("dump.dmp", "journal.log", "config.conf"):
            with self.subTest(nom=nom):
                self.assertFalse(
                    (self.cible / nom).exists(),
                    f"{nom} ne doit pas etre livre",
                )

    def test_le_code_python_compile_est_ecarte(self) -> None:
        """`__pycache__` et les `.pyc` ne sont pas livres.

        Un `.pyc` livre est un fichier qui ne correspond plus au `.py`
        qu'il compile, et qui peut faire tourner du code obsolete.
        """
        self.assertFalse((self.cible / "pycache").exists())
        self.assertEqual(list(self.cible.rglob("*.pyc")), [])

    def test_la_copie_ne_contient_pas_de_git(self) -> None:
        """Le resultat est une copie, pas un clone.

        Il ne doit pas embarquer `.git` : le destinataire doit pouvoir
        decider de son propre depot, et un `.git` copie transport un
        historique et une configuration de remote.
        """
        self.assertFalse((self.cible / ".git").exists())


class TestProtectionDuDepot(unittest.TestCase):
    """Ce qui protege le depot d'une mauvaise invocation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_la_copie_dans_le_depot_est_refusee(self) -> None:
        """Copier le depot sur lui-meme est refuse.

        Chaque fichier serait copie sur lui-meme : aucun gain, et un
        journal qui laisse croire a un travail reel. Le refus doit
        survenir *avant* toute lecture, et dire *pourquoi*, plutot que
        de laisser `shutil` echouer plus tard sur une erreur technique.
        """
        depot = _depot_jetable(self.base)
        temoin = depot / "lu.txt"
        avant = temoin.read_bytes()

        rc = _executer(depot, depot, par_main=True)
        with self.subTest("refus"):
            self.assertNotEqual(rc.returncode, 0, rc.stdout)
            self.assertIn("depot", (rc.stdout + rc.stderr).lower())
        with self.subTest("fichier source intact"):
            self.assertEqual(temoin.read_bytes(), avant)

    def test_le_refus_tient_meme_avec_ecrasement(self) -> None:
        """`--overwrite` ne contourne pas le refus de l'auto-copie.

        C'est le cas que l'option rend dangereux : elle autorise
        l'ecrasement, donc une invocation `generate_project.py .
        --overwrite` atteint tout. Le garde-fou doit se poser avant
        l'analyse de l'option, sinon l'utilisateur qui ajoute
        `--overwrite` pour etre sur d'ecraser peut vider le depot.
        """
        depot = _depot_jetable(self.base)
        lu = depot / "lu.txt"
        avant = lu.read_bytes()

        rc = _executer(depot, depot, par_main=True, extra=["--overwrite"])
        with self.subTest("refus"):
            self.assertNotEqual(rc.returncode, 0, rc.stdout)
        with self.subTest("fichier source intact"):
            self.assertEqual(lu.read_bytes(), avant)

    def test_un_destinataire_existant_n_est_pas_ecrase(self) -> None:
        """Sans `--overwrite`, un fichier deja present est conserve.

        Ecraser par defaut punirait un destinataire qui avait ajoute un
        fichier, ou lance le script deux fois. L'ecrasement doit etre
        demande, doncimation.
        """
        depot = _depot_jetable(self.base)
        cible = self.base / "copie"
        cible.mkdir()
        temoin = cible / "lu.txt"
        temoin.write_text("VERSION LOCALE\n", encoding="utf-8")

        rc = _executer(depot, cible)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        with self.subTest("fichier conserve"):
            self.assertEqual(
                temoin.read_text(encoding="utf-8"), "VERSION LOCALE\n"
            )
        with self.subTest("les autres fichiers sont quand meme copies"):
            self.assertTrue((cible / "src" / "module.py").is_file())

    def test_les_fichiers_ignores_sont_annonces(self) -> None:
        """Un fichier non copie est dit, et compte.

        Un ecrasement silencieux laisserait croire a une copie complete
        alors qu'elle ne l'est pas.
        """
        depot = _depot_jetable(self.base)
        cible = self.base / "copie"
        cible.mkdir()
        (cible / "lu.txt").write_text("existant\n", encoding="utf-8")

        rc = _executer(depot, cible)
        self.assertIn("lu.txt", rc.stdout)
        self.assertIn("IGNORER", rc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
