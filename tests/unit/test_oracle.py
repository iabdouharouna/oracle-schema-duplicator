"""Adaptateur Oracle : ce qui decide de la place demandee, et quand.

Le filtrage de tables pose deux questions a `schema_segment_bytes`, et
les deux se paient differemment :

* **rien n'est filtre** — la somme SQL suffit, une requete, et ce chemin
  ne doit jamais se transformer en lecture de tous les segments. C'est le
  cas de tres loin le plus courant, et le seul qui n'a aucun plafond a
  craindre ;
* **des objets sont filtres** — le retranchement ne peut pas rester dans
  le SQL. Une clause `segment_name in (...)` portant des milliers de noms
  atteint les 1000 expressions d'Oracle (`ORA-01795`) et fait, sur une
  seule ligne de script, ce que le shell distant ne survit pas. Les
  segments sont donc lus en entier et recoupes ici.

Ces tests tiennent le second point hors de portee du premier : ils
verifient **quelle** methode est appelee, et non seulement ce qui est
rendu. Un `if not exclus:` ecrit sur un dictionnaire toujours non vide
ferait retomber le cas sans filtre sur la lecture complete — le resultat
resterait juste, et le cout non.

L'adaptateur reel n'est pas joint a une base : `scalar` et `query` sont
remplaces par des doubles. Ce qui est teste est la decision, pas la
connexion.
"""

from __future__ import annotations

import unittest

import support  # noqa: F401

from osd.adapters.oracle import (
    _SEGMENT_FAMILLES,
    _segment_exclu,
    _segment_exclusions,
    _to_int,
    OracleAdapter,
    OracleSide,
)


class _AdaptateurDOracle(OracleAdapter):
    """Adaptateur dont les deux lectures SQL sont observees, pas jouees."""

    def __init__(self, *, somme: str = "1000", segments=None) -> None:
        super().__init__(
            OracleSide(name="s", connect="db", schema="SRC", directory="/d")
        )
        self.somme = somme
        self.segments = segments or []
        self.appels = []

    def scalar(self, sql: str, *, timeout: int = 0) -> str:
        self.appels.append(("scalar", sql))
        return self.somme

    def query(self, sql: str, *, timeout: int = 0):
        self.appels.append(("query", sql))
        return self.segments

    def methodes(self):
        return [nom for nom, _ in self.appels]


class TestToInt(unittest.TestCase):
    """Le scalaire de SQL*Plus, lu pour ce qu'il est."""

    def test_un_entier_ordinaire(self):
        self.assertEqual(_to_int("12"), 12)

    def test_la_notation_scientifique_est_un_nombre(self):
        """`sum(bytes)` rend `1.0369E+11`, pas `103690000000`.

        SQL*Plus ajuste le format a la largeur de colonne des que le
        nombre est grand. Lire par `int` seul echouait, et l'echec
        silencieux valait **zero** : l'estimation d'espace d'un schema de
        103 Go etait nulle, et la place disponible n'etait plus verifiee
        du tout. C'est le sens dangereux — celui d'une place qu'on croit
        suffisante parce qu'on ne mesure rien.
        """
        self.assertEqual(_to_int("1.0369E+11"), 103_690_000_000)
        self.assertEqual(_to_int(" 2.5E+03 "), 2500)

    def test_vide_et_illisible_valent_zero(self):
        self.assertEqual(_to_int(""), 0)
        self.assertEqual(_to_int(None), 0)
        self.assertEqual(_to_int("inconnu"), 0)


class TestFamillesDeSegments(unittest.TestCase):
    """Le type d'objet range le nom, et un type inconnu ne range rien."""

    def test_le_type_d_objet_choisit_la_famille(self):
        familles = _segment_exclusions(
            ["TABLE~T", "INDEX~I", "LOB~L", "TABLE PARTITION~P"]
        )
        self.assertEqual(familles["TABLE"], {"T", "P"})
        self.assertEqual(familles["INDEX"], {"I"})
        self.assertEqual(familles["LOB"], {"L"})

    def test_un_type_inconnu_est_ignore(self):
        """Le laisser dans le total est l'erreur prudente : on sur-estime."""
        familles = _segment_exclusions(["PACKAGE~PKG"])
        self.assertEqual(familles["TABLE"], set())
        self.assertEqual(familles["INDEX"], set())
        self.assertEqual(familles["LOB"], set())

    def test_un_nom_vide_est_ignore(self):
        self.assertEqual(_segment_exclusions(["TABLE~"])["TABLE"], set())

    def test_chaque_famille_liste_ses_types_de_segments(self):
        """Les types de `DBA_SEGMENTS` ne sont pas ceux de `DBA_OBJECTS`.

        Un segment de partition porte le nom de l'objet, et les LOB sont
        `LOBSEGMENT`/`LOBINDEX` cote segments. Confondre les deux
        laisserait des segments dans le total — une estimation trop
        haute, donc un refus inutile, sans message qui l'explique.
        """
        types = {cle: set(t) for cle, t in _SEGMENT_FAMILLES}
        self.assertIn("TABLE PARTITION", types["TABLE"])
        self.assertIn("LOBINDEX", types["INDEX"])
        self.assertIn("LOBSEGMENT", types["LOB"])
        # Aucune famille ne partage un type avec une autre : c'est ce qui
        # rend le retranchement non ambigu.
        vus = set()
        for famille in types.values():
            self.assertFalse(vus & famille)
            vus |= famille


class TestSegmentExclu(unittest.TestCase):
    """Le nom seul ne suffit pas : le type doit concorder."""

    def setUp(self):
        self.familles = _segment_exclusions(["TABLE~PARTAGE", "INDEX~IDX"])

    def test_le_type_et_le_nom_doivent_concorder(self):
        self.assertTrue(_segment_exclu("TABLE", "PARTAGE", self.familles))
        self.assertTrue(_segment_exclu("INDEX", "IDX", self.familles))

    def test_un_index_homonyme_d_une_table_est_conserve(self):
        """Retrancher par le nom seul retirerait l'index d'une autre table.

        Les noms circulent librement dans un schema : une table filtree et
        un index sans rapport peuvent s'appeler pareil. Le total serait
        alors ampute d'octets qui voyagent vraiment, sans qu'aucun message
        ne rattache l'ecart a sa cause.
        """
        self.assertFalse(_segment_exclu("INDEX", "PARTAGE", self.familles))
        self.assertFalse(_segment_exclu("TABLE", "IDX", self.familles))

    def test_un_segment_inconnu_est_conserve(self):
        self.assertFalse(_segment_exclu("TABLE", "AUTRE", self.familles))


class TestSchemaSegmentBytes(unittest.TestCase):
    """Quel chemin est emprunte, selon qu'il y a des exclusions ou non."""

    def test_sans_exclusion_une_seule_somme_sql(self):
        ad = _AdaptateurDOracle(somme="103690000000")
        octets = ad.schema_segment_bytes("SRC", content="ALL")
        self.assertEqual(octets, 103_690_000_000)
        self.assertEqual(ad.methodes(), ["scalar"])

    def test_sans_exclusion_la_notation_scientifique_est_lue(self):
        ad = _AdaptateurDOracle(somme="1.0369E+11")
        self.assertEqual(ad.schema_segment_bytes("SRC", content="ALL"),
                         103_690_000_000)

    def test_avec_exclusion_les_segments_sont_lus_entierement(self):
        ad = _AdaptateurDOracle(segments=[
            ["TABLE", "T1", "500"],
            ["TABLE", "T2", "700"],
            ["INDEX", "T1", "300"],
        ])
        octets = ad.schema_segment_bytes(
            "SRC", content="ALL",
            segment_signatures=["TABLE~T1"],
        )
        # `T1` (table) est retire ; l'index homonyme reste.
        self.assertEqual(octets, 1000)
        self.assertEqual(ad.methodes(), ["query"])

    def test_le_retranchement_ne_demande_aucune_liste_a_oracle(self):
        """La table filtree ne doit figurer dans aucune requete.

        C'est la condition de surete : une liste de plusieurs milliers de
        noms atteint ORA-01795 et fait une ligne de script que le shell
        distant ne survit pas.
        """
        ad = _AdaptateurDOracle(segments=[["TABLE", "T1", "500"]])
        ad.schema_segment_bytes(
            "SRC", content="ALL",
            segment_signatures=[f"TABLE~T{i}" for i in range(5000)],
        )
        (_, sql), = ad.appels
        self.assertNotIn("T4999", sql)
        self.assertNotIn(" in (", sql.lower())

    def test_metadata_only_ne_lit_aucun_segment(self):
        ad = _AdaptateurDOracle(somme="999")
        ad.object_count = lambda schema, **kw: 10
        ad.schema_segment_bytes(
            "SRC", content="METADATA_ONLY",
            segment_signatures=["TABLE~T1"],
        )
        self.assertEqual(ad.appels, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
