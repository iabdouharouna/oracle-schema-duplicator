"""Tests du filtre de masquage des secrets.

C'est le test le plus important du projet en termes de consequence : une
fuite de mot de passe dans un journal est irrattrapable, et un journal
est lu par des gens qui n'ont pas les memes droits que l'exploitant.
Chaque forme reelle de secret Oracle est donc verifiee ici, y compris
celles que le projet ne produit pas lui-meme mais qu'il peut relire —
typiquement le DDL d'un dump, qui contient des `IDENTIFIED BY VALUES`.
"""

from __future__ import annotations

import unittest

import support  # noqa: F401  (installe src/ dans sys.path)

from osd import redact as rd


class TestChaineDeConnexion(unittest.TestCase):
    def test_masque_le_mot_de_passe_dans_une_chaine_easyconnect(self):
        texte = "ORA-01017: connexion pour l'utilisateur HR/MotDepasse123 @L_PROD"
        self.assertNotIn("MotDepasse123", rd.redact(texte))
        self.assertIn(rd.MASK, rd.redact(texte))

    def test_conserve_le_nom_d_utilisateur(self):
        """Masquer le secret ne doit pas rendre le diagnostic inexploitable.

        L'exploitant a besoin de savoir *quel* compte a echoue ; c'est le
        mot de passe qui est confidentiel, pas l'identite du compte.
        """
        sortie = rd.redact("user1/secret1@host:1521/svc")
        self.assertIn("user1", sortie)
        self.assertNotIn("secret1", sortie)

    def test_chaine_sans_mot_de_passe_inchangee(self):
        for texte in ("user1@host:1521/svc", "L_PROD", "//h:1521/SVC", "/@L_PROD"):
            with self.subTest(texte=texte):
                self.assertEqual(rd.redact(texte), texte)


class TestIdentifiedBy(unittest.TestCase):
    def test_masque_un_mot_de_passe_en_clair(self):
        texte = "CREATE USER \"HR\" IDENTIFIED BY UnMotDePasse"
        self.assertNotIn("UnMotDePasse", rd.redact(texte))

    def test_masque_une_empreinte_values(self):
        """Forme produite par le DDL de Data Pump — la plus facile a manquer.

        L'empreinte `S:...;T:...` n'est pas un mot de passe, mais elle
        permet de retrouver le mot de passe par force brute hors ligne.
        Une empreinte en clair dans un journal est donc une fuite au même
        titre qu'un mot de passe en clair.
        """
        texte = (
            'CREATE USER "HR" IDENTIFIED BY VALUES '
            "'S:B031DD6F5673B7D3A1;T:AA9F0A6A0F0F0B0C0D0E0F1011121314151617'"
        )
        sortie = rd.redact(texte)
        self.assertNotIn("B031DD6F5673B7D3A1", sortie)
        self.assertNotIn("AA9F0A6A0F0F0B0C0D0E0F", sortie)
        self.assertIn(rd.MASK, sortie)

    def test_masque_une_empreinte_values_doublement_quotee(self):
        texte = "IDENTIFIED BY VALUES 'S:AA;T:BB'"
        self.assertNotIn("S:AA", rd.redact(texte))

    def test_masque_toutes_les_occurrences_d_un_ddl_complet(self):
        """Sur le DDL d'un vrai dump, il y a un `IDENTIFIED BY` par objet.

        Tester sur un seul cas laisserait passer une regex non globale ;
        c'est le piege classique de ce type de motif.
        """
        ddl = (
            'CREATE USER "HR" IDENTIFIED BY VALUES \'S:AAAA;T:BBBB\';\n'
            'ALTER USER "HR" IDENTIFIED BY VALUES \'S:CCCC;T:DDDD\';\n'
            "CREATE TABLE \"EMPLOYEES\" (\"EMPLOYEE_ID\" NUMBER);\n"
        )
        sortie = rd.redact(ddl)
        for secret in ("S:AAAA", "T:BBBB", "S:CCCC", "T:DDDD"):
            self.assertNotIn(secret, sortie)
        # Le DDL non secret doit survivre intact : un redactor trop
        # agressif rend le rapport illisible sans rien securiser de plus.
        self.assertIn("CREATE TABLE", sortie)
        self.assertIn("EMPLOYEE_ID", sortie)


class TestAffectationsEtParfiles(unittest.TestCase):
    def test_masque_password_dans_un_parfile(self):
        for ligne in (
            'userid="TOTO/Secrete123@L_PROD"',
            "userid=TOTO/Secrete123@L_PROD",
            "password=Secrete123",
            "passwd=Secrete123",
            "pwd=Secrete123",
        ):
            with self.subTest(ligne=ligne):
                self.assertNotIn("Secrete123", rd.redact(ligne))

    def test_masque_le_pragma_sqlnet(self):
        texte = 'SQLNET.WALLET_LOCATION=(SOURCE=(METHOD=FILE)(DIRECTORY=/w))(SECRET=Abc123)'
        self.assertNotIn("Abc123", rd.redact(texte))


class TestRobustesse(unittest.TestCase):
    def test_ne_leve_jamais(self):
        """La redaction ne doit pas pouvoir devenir une panne.

        Elle est appelee dans le formateur de logs, donc dans un `except`.
        Une exception ici transformerait un echec metier en erreur
        technique, et le message d'origine — celui qui explique — serait
        perdu.
        """
        for valeur in (None, 123, [], {}, object(), b"octets", "\x00\xff"):
            with self.subTest(valeur=type(valeur).__name__):
                self.assertIsInstance(rd.redact(valeur), str)

    def test_texte_vide_ou_sans_secret_inchange(self):
        for texte in ("", "expdp并行 PARALLEL=4", "aucun secret ici"):
            with self.subTest(texte=texte):
                self.assertEqual(rd.redact(texte), texte)

    def test_argv_redacte(self):
        argv = ["expdp", "userid=SECRET/abc@L_P", "DIRECTORY=DP"]
        sortie = rd.redact_argv(argv)
        self.assertNotIn("abc", " ".join(sortie))
        self.assertTrue(any("DIRECTORY=DP" in s for s in sortie))


if __name__ == "__main__":
    unittest.main()
