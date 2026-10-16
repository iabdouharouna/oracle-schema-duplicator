"""Filtrage des tables : resolution du plan et formes envoyees a Data Pump.

Le module teste ici ne fait que trois choses, et chacune est un lieu ou
l'outil peut mentir sans que rien d'autre le revelle :

* **designer** les tables. Une entree sans correspondance n'est pas un
  echec, mais silencieusement laisser partir le schema entier le serait ;
* **fermer** : retirer avec une table ce qui depend d'elle. Creer une vue
  sur une table absente echoue en `ORA-00942` a l'import, et l'echec
  n'apparait qu'une fois l'export deja fait et transfere ;
* **ecrire** les clauses. En 19c il n'existe aucun separateur entre
  filtres : `exclude=USER;TABLE:"..."` est refuse en `ORA-39038`, lu comme
  un type d'objet `USER;TABLE` inexistant.

Le faux schema est declare **une fois** ci-dessous, avec une structure
volontairement riche : un mode A, un mode B, une cascade d'index et de
triggers, et deux niveaux de dependance (une vue, puis une vue sur la
vue). Une fermeture qui ne ferait qu'un tour passerait les tests les plus
simples et echouerait sur le troisieme objet cree.
"""

from __future__ import annotations

import unittest

import support  # noqa: F401

from osd.checks.table_filter import (
    TableFilterPlan,
    nom_de_signature,
    plan_table_filter,
    signature,
)
from osd.errors import ConfigError

#: Tables du faux schema.
_TABLES = [
    "DEPARTMENTS", "EMPLOYEES", "REGIONS", "SAL_HISTORY", "TMP_LOAD",
]

#: Ce qui part **avec** chaque table — la cascade observee de
#: `EXCLUDE=TABLE` (index, triggers, LOB), la table elle-meme comprise.
_POSSEDES = {
    "EMPLOYEES": ["EMPLOYEES", "EMP_EMP_ID_PK", "IX_EMP_DEPT", "TRG_EMP"],
    "SAL_HISTORY": ["SAL_HISTORY", "SAL_HIST_PK"],
    "TMP_LOAD": ["TMP_LOAD"],
}

#: Ce qui reference quoi. `EMPLOYEES` est referencee par `V_EMP` et par
#: `PKG_EMP`, et `V_EMP` l'est a son tour par `V_EMP_SAL` : la fermeture
#: doit faire deux tours pour tout trouver.
_DEPENDANTS = {
    "EMPLOYEES": ["V_EMP", "PKG_EMP"],
    "V_EMP": ["V_EMP_SAL"],
    "PKG_EMP": [],
    "V_EMP_SAL": [],
}

#: Inventaire du schema, ordonne comme la requete le rend
#: (`order by object_type, object_name`) : c'est l'ordre des signatures
#: que le plan recoit, et il est donc deterministe.
_SIGNATURES = [
    ["INDEX", "EMP_EMP_ID_PK"],
    ["INDEX", "IX_EMP_DEPT"],
    ["INDEX", "SAL_HIST_PK"],
    ["PACKAGE", "PKG_EMP"],
    ["PACKAGE BODY", "PKG_EMP"],
    ["TABLE", "DEPARTMENTS"],
    ["TABLE", "EMPLOYEES"],
    ["TABLE", "REGIONS"],
    ["TABLE", "SAL_HISTORY"],
    ["TABLE", "TMP_LOAD"],
    ["TRIGGER", "TRG_EMP"],
    ["VIEW", "EMP_DETAILS_VIEW"],
    ["VIEW", "V_EMP"],
    ["VIEW", "V_EMP_SAL"],
]


def _faux(**donnees):
    """Adaptateur source du faux schema, surchargeable par le test."""
    donnees.setdefault("tables", _TABLES)
    donnees.setdefault("possedes", _POSSEDES)
    donnees.setdefault("dependants", _DEPENDANTS)
    donnees.setdefault("signatures", _SIGNATURES)
    return support.SourceFiltre(**donnees)


class TestSansFiltre(unittest.TestCase):
    """Le cas par defaut : rien n'est configure, rien ne doit changer."""

    def test_un_plan_vide_ne_consulte_la_base(self):
        """Un run sans filtre ne doit pas interroger `DBA_OBJECTS`.

        Ce n'est pas une optimisation : c'est la condition pour qu'un
        schema sans droits sur les vues de dependance, ou une base ou le
        filtrage n'existe pas, continue de fonctionner exactement comme
        avant la fonctionnalite.
        """
        faux = _faux()
        plan = plan_table_filter(faux, "SRC")
        self.assertEqual(faux.queries, [])
        self.assertFalse(plan.active)
        self.assertEqual(plan.excluded, [])
        self.assertEqual(plan.data_only, [])
        self.assertEqual(plan.unmatched, [])

    def test_une_entree_vide_ne_filtre_rien_et_ne_signale_rien(self):
        """`EXCLUDE_TABLES=` n'a ni de volonté ni de contrepartie.

        Un avertissement ici designerait une entree que personne n'a
        ecrite — la ligne existe, sa valeur est vide.
        """
        plan = plan_table_filter(_faux(), "SRC", tables=["", "   "])
        self.assertFalse(plan.active)
        self.assertEqual(plan.unmatched, [])


class TestNomsExacts(unittest.TestCase):
    def test_les_noms_sont_insensibles_a_la_casse_et_rendus_en_base(self):
        """La configuration s'ecrit comme on la voit, la base comme elle est.

        Oracle stocke `EMPLOYEES`, l'exploitant peut ecrire `employees`.
        Rendre le nom de la base — et non celui de la configuration —
        fait que la clause produite est celle qui existe vraiment, et non
        une chaine dont personne ne peut verifier la casse.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables=["employees", "sal_history"])
        self.assertEqual(plan.excluded, ["EMPLOYEES", "SAL_HISTORY"])
        self.assertTrue(plan.active)

    def test_une_entree_inconnue_est_listee_et_ne_filtre_rien(self):
        """La faute de frappe doit se voir dans le rapport.

        `EMPLOYEE` n'existe pas : la table part integralement, et rien
        ne l'en empecherait. L'entree est donc conservee, avec le nom de
        sa cle, pour que le message dise quoi corriger.
        """
        plan = plan_table_filter(_faux(), "SRC", tables=["EMPLOYEE"])
        self.assertFalse(plan.active)
        self.assertEqual(plan.excluded, [])
        self.assertEqual(plan.unmatched, ["EXCLUDE_TABLES=EMPLOYEE"])

    def test_une_entree_de_l_autre_mode_signe_son_propre_nom(self):
        plan = plan_table_filter(_faux(), "SRC",
                                 tables_data=["REGIONS_VIDE"])
        self.assertEqual(plan.unmatched, ["EXCLUDE_TABLES_DATA=REGIONS_VIDE"])


class TestExpressionsRegulieres(unittest.TestCase):
    def test_un_motif_designe_plusieurs_tables_d_un_coup(self):
        """Le motif est cherche dans le nom reel, ignore la casse.

        `^tmp_` ne connait pas la casse d'ecriture de `TMP_LOAD` — et il
        ne doit pas la connaitre : une base Oracle en majuscules par
        defaut rendrait tout motif ecrit en minuscules silencieusement
        inactif.
        """
        plan = plan_table_filter(_faux(), "SRC", tables_regex=["^tmp_"])
        self.assertEqual(plan.excluded, ["TMP_LOAD"])

    def test_un_motif_peut_viser_le_mode_sans_donnees(self):
        plan = plan_table_filter(_faux(), "SRC",
                                 tables_data_regex=["history$"])
        self.assertEqual(plan.data_only, ["SAL_HISTORY"])

    def test_un_motif_sans_correspondance_est_signale(self):
        """Un motif juste qui ne designe plus rien change de sens.

        La table a peut-etre ete renommee entre deux runs : le motif est
        alors correct et la donnee ne l'est plus, ce qui n'a qu'une
        lecture possible — l'entree, pas le schema.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables_regex=["^zzz"])
        self.assertEqual(plan.unmatched, ["EXCLUDE_TABLES_REGEX=^zzz"])
        self.assertFalse(plan.active)

    def test_une_regex_mal_formee_est_refusee(self):
        """Le refus porte le nom de la cle, comme au chargement.

        Deux diagnostics differents pour une meme faute obligent a
        choisir lequel croire. La configuration valide deja ces motifs ;
        cette seconde compilation est un filet, et un filet qui parle
        d'une autre cle que la porte n'aide personne.
        """
        for cle, argument, valeur in (
            ("EXCLUDE_TABLES_REGEX", "tables_regex", "["),
            ("EXCLUDE_TABLES_DATA_REGEX", "tables_data_regex", "a{2,1}"),
        ):
            with self.subTest(cle=cle):
                with self.assertRaises(ConfigError) as ctx:
                    plan_table_filter(_faux(), "SRC",
                                      **{argument: [valeur]})
                self.assertIn(cle, str(ctx.exception))


class TestChevauchement(unittest.TestCase):
    def test_lexclusion_totale_prime_sur_le_mode_sans_donnees(self):
        """Une table declaree deux fois n'a qu'une realite possible.

        Exporter une table hors duplication **et** sans donnees est
        contradictoire. L'exclusion totale gagne, et l'autre entree
        disparait : produire les deux lignes dans le parfile donnerait un
        `query=` sur une table que l'`exclude=` vient d'ecarter — une
        erreur Data Pump dont le message ne designerait aucune des deux
        cles.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables=["REGIONS"],
                                 tables_data=["REGIONS"])
        self.assertEqual(plan.overlap, ["REGIONS"])
        self.assertEqual(plan.excluded, ["REGIONS"])
        self.assertEqual(plan.data_only, [])
        self.assertTrue(plan.active)


class TestFermeture(unittest.TestCase):
    """Ce qui part avec une table, et ce qui en depend."""

    def _mode_a(self) -> TableFilterPlan:
        return plan_table_filter(_faux(), "SRC", tables=["EMPLOYEES"])

    def test_les_dependants_sont_retires_transitivement(self):
        """Deux tours de fermeture, pas un.

        `V_EMP` depend d'`EMPLOYEES` : la retirer, oui. `V_EMP_SAL`
        depend de `V_EMP` — creer la deuxieme sans la premiere echoue
        aussi, et echoue plus tard, quand le dump est deja lu. Un
        s'arretant au premier tour laisserait donc un echec d'import
        dont rien ne dirait qu'il vient d'un filtre.
        """
        plan = self._mode_a()
        self.assertEqual(
            set(plan.dependents),
            {"VIEW~V_EMP", "PACKAGE~PKG_EMP", "PACKAGE BODY~PKG_EMP",
             "VIEW~V_EMP_SAL"},
        )

    def test_la_cascade_et_les_dependants_ont_chacun_leur_rang(self):
        """Les index et le trigger partent par la cascade, pas par dependance.

        `EMP_EMP_ID_PK` et `TRG_EMP` n'ont aucune ligne dans
        `DBA_DEPENDANTS` a leur nom : ils partent avec la table. Les
        deux chemins aboutissent a `ignored` — c'est ce qui compte pour
        l'etape 16 — mais `dependents` doit ne contenir que les seconds,
        sinon le rapport annoncerait « 4 objets dependants retire(s) »
        alors qu'un index n'est dependent de rien.
        """
        plan = self._mode_a()
        self.assertIn("INDEX~EMP_EMP_ID_PK", plan.ignored)
        self.assertIn("TRIGGER~TRG_EMP", plan.ignored)
        self.assertNotIn("INDEX~EMP_EMP_ID_PK", plan.dependents)
        self.assertNotIn("TRIGGER~TRG_EMP", plan.dependents)

    def test_les_segments_des_deux_modes_quittent_le_compte(self):
        """L'estimation d'espace ne doit pas exiger la place des segments
        qui ne voyagent pas.

        Mode A et mode B partagent la meme regle pour les segments —
        aucune donnee ne part — mais pas la meme pour les objets : la
        table du mode B est dupliquee, donc presente dans l'inventaire
        compare a l'etape 16.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables=["EMPLOYEES"],
                                 tables_data=["SAL_HISTORY"])
        self.assertEqual(
            set(plan.segment_ignored),
            {"TABLE~EMPLOYEES", "INDEX~EMP_EMP_ID_PK", "INDEX~IX_EMP_DEPT",
             "TRIGGER~TRG_EMP", "TABLE~SAL_HISTORY", "INDEX~SAL_HIST_PK"},
        )
        self.assertNotIn("TABLE~SAL_HISTORY", plan.ignored)

    def test_le_mode_sans_donnees_ne_retire_aucun_objet(self):
        """La table du mode B est dupliquee : elle doit rester dans la
        comparaison.

        La retirer de l'inventaire source ferait que l'etape 16 ne
        verrait jamais son absence — c'est-a-dire qu'une table oubliee
        par l'import passerait pour une reussite.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables_data=["SAL_HISTORY"])
        self.assertEqual(plan.ignored, [])
        self.assertEqual(
            set(plan.segment_ignored),
            {"TABLE~SAL_HISTORY", "INDEX~SAL_HIST_PK"},
        )
        self.assertTrue(plan.active)


class TestClausesDeParfile(unittest.TestCase):
    def test_une_seule_clause_couvre_toutes_les_tables(self):
        """`IN` avec des virgules : c'est la syntaxe SQL, pas un separateur.

        La liste interne separee par un autre caractere que la virgule
        serait lue par Oracle comme un nom incomplet, et la table
        entiere filtree — en silence, l'export reussissant.
        """
        plan = TableFilterPlan(schema="SRC",
                               excluded=["EMPLOYEES", "TMP_LOAD"])
        self.assertEqual(
            plan.exclude_clause(),
            'TABLE:"IN (\'EMPLOYEES\', \'TMP_LOAD\')"',
        )

    def test_sans_exclusion_la_clause_est_absente(self):
        """Un `exclude=` vide est une erreur de syntaxe, pas un filtre nul."""
        plan = TableFilterPlan(schema="SRC", data_only=["SAL_HISTORY"])
        self.assertEqual(plan.exclude_clause(), "")

    def test_les_guillemets_des_noms_sont_doubles(self):
        """Un nom entre quotes doit survivre au parfile.

        `'` est echappee pour SQL, `"` pour le quoteur de Data Pump.
        L'oubli de la seconde coupe la clause au milieu : Oracle lit
        alors un filtre different de celui qui est annonce, sans erreur.
        """
        plan = TableFilterPlan(schema="SRC",
                               excluded=["A\"B", "O'Brien"])
        self.assertEqual(
            plan.exclude_clause(),
            'TABLE:"IN (\'A""B\', \'O\'\'Brien\')"',
        )

    def test_une_ligne_de_query_par_table_sans_donnees(self):
        """La valeur est nue : le prefixe `query=` appartient a
        l'adaptateur Data Pump, pas au plan de filtrage."""
        plan = TableFilterPlan(schema="SRC",
                               data_only=["DEPARTMENTS", "REGIONS"])
        self.assertEqual(
            plan.query_lines(),
            [
                'SRC.DEPARTMENTS:"WHERE 1=2"',
                'SRC.REGIONS:"WHERE 1=2"',
            ],
        )

    def test_un_nom_simple_est_nu_et_un_nom_exotique_est_quote(self):
        """L'identifiant n'est quote que si la forme l'exige.

        `SRC.EMPLOYEES` se lit ; `SRC.ma table` serait lu comme deux
        parametres. Inverse, quotifier partout produirait un identifiant
        sensible a la casse la ou Oracle en acceptait un sans quotes.
        """
        nu = TableFilterPlan(schema="SRC", data_only=["EMPLOYEES"])
        quote = TableFilterPlan(schema="SRC", data_only=["Ma Table"])
        self.assertEqual(nu.query_lines(),
                         ['SRC.EMPLOYEES:"WHERE 1=2"'])
        self.assertEqual(quote.query_lines(),
                         ['SRC."Ma Table":"WHERE 1=2"'])

    def test_sans_mode_b_il_n_y_a_aucune_query(self):
        plan = TableFilterPlan(schema="SRC", excluded=["EMPLOYEES"])
        self.assertEqual(plan.query_lines(), [])


class TestTablesIsolees(unittest.TestCase):
    """`EXCLUDE_STANDALONE_TABLES` : une regle qui alimente le mode B.

    La definition a trois volets, et chacun suffit a **ecarter** une
    table : une FK sortante, une FK entrante d'un schema quelconque, ou
    un objet non-table qui la lit. Les volets sont verifies separement —
    une table liee d'un seul cote, ou lue par une seule vue, n'est pas
    isolee, et un test qui n'en regarderait qu'un declarerait isolee une
    table que le schema utilise.
    """

    def test_sans_fk_ni_usage_toutes_les_tables_sont_isolees(self):
        """Aucune relation, aucun lecteur : toutes basculent en mode B.

        `dependants` est vide ici : aucune vue, aucun package ne lit ces
        tables. C'est ce second silence, autant que l'absence de FK, qui
        autorise a retirer leurs lignes.
        """
        plan = plan_table_filter(_faux(dependants={}), "SRC", standalone=True)
        self.assertTrue(plan.standalone_requested)
        self.assertEqual(plan.standalone, sorted(_TABLES))
        self.assertEqual(plan.data_only, sorted(_TABLES))
        self.assertEqual(plan.excluded, [])
        self.assertTrue(plan.active)

    def test_un_objet_non_table_ecarte_la_table(self):
        """Une table lue par une vue ou un package n'est pas morte.

        `EMPLOYEES` n'est liee par aucune FK — mais `V_EMP` (vue) et
        `PKG_EMP` (package) la lisent. Retirer ses lignes viderait une
        donnee attendue ; la table part donc en entier, et seule
        l'absence des deux autres lecteurs les laisse isolees.
        """
        plan = plan_table_filter(_faux(), "SRC", standalone=True)
        self.assertNotIn("EMPLOYEES", plan.standalone)
        self.assertEqual(
            plan.standalone,
            ["DEPARTMENTS", "REGIONS", "SAL_HISTORY", "TMP_LOAD"],
        )

    def test_un_trigger_propre_ne_disqualifie_pas(self):
        """Le trigger pose sur la table elle-meme n'est pas un lecteur.

        Il fait partie de son DDL et voyage avec elle : une table sans FK
        dont le seul « utilisateur » est son propre trigger reste isolee.
        """
        faux = _faux(
            possedes={**_POSSEDES,
                      "SAL_HISTORY": ["SAL_HISTORY", "SAL_HIST_PK", "TRG_SAL"]},
            dependants={"SAL_HISTORY": ["TRG_SAL"]},
            signatures=_SIGNATURES + [["TRIGGER", "TRG_SAL"]],
        )
        plan = plan_table_filter(faux, "SRC", standalone=True)
        self.assertIn("SAL_HISTORY", plan.standalone)

    def test_un_trigger_d_une_autre_table_disqualifie(self):
        """Un trigger d'une **autre** table, dont le corps lit celle-ci, la
        disqualifie.

        Le trigger part avec la table qui le porte, pas avec celle qu'il
        lit : retirer les lignes de cette derniere viderait une donnee
        que le trigger manipule.
        """
        faux = _faux(
            possedes={**_POSSEDES,
                      "AUTRE": ["AUTRE", "TRG_AUTRE"]},
            dependants={"SAL_HISTORY": ["TRG_AUTRE"]},
            signatures=_SIGNATURES + [["TABLE", "AUTRE"],
                                      ["TRIGGER", "TRG_AUTRE"]],
        )
        plan = plan_table_filter(faux, "SRC", standalone=True)
        self.assertNotIn("SAL_HISTORY", plan.standalone)

    def test_un_dependant_de_type_table_ne_disqualifie_pas(self):
        """Un lien table-a-table sans FK ne rend pas la table « utilisee ».

        C'est le volet referentiel qui tranche ces liens : compter ici une
        dependance dont le referenceur est lui-meme une table rendrait la
        regle dependante d'une seconde lecture des FK.
        """
        faux = _faux(
            dependants={"REGIONS": ["OTHER_TABLE"]},
            signatures=_SIGNATURES + [["TABLE", "OTHER_TABLE"]],
        )
        plan = plan_table_filter(faux, "SRC", standalone=True)
        self.assertIn("REGIONS", plan.standalone)

    def test_une_fk_sortante_ecarte_la_table(self):
        """Une table qui reference est liee, meme si personne ne la cite."""
        plan = plan_table_filter(
            _faux(fk_sortantes=["EMPLOYEES"]), "SRC", standalone=True
        )
        self.assertNotIn("EMPLOYEES", plan.standalone)
        self.assertEqual(
            plan.standalone,
            ["DEPARTMENTS", "REGIONS", "SAL_HISTORY", "TMP_LOAD"],
        )

    def test_une_fk_entrante_ecarte_la_table(self):
        """Une table referencee est liee, meme si elle ne reference rien.

        C'est la moitie que le SQL du besoin exprime par la jointure sur
        `r_constraint_name` : la FK peut venir d'un autre schema, la table
        n'en est pas moins attendue.
        """
        plan = plan_table_filter(
            _faux(fk_entrantes=["DEPARTMENTS"]), "SRC", standalone=True
        )
        self.assertNotIn("DEPARTMENTS", plan.standalone)
        self.assertIn("REGIONS", plan.standalone)

    def test_un_schema_entierement_relie_ne_filtre_rien_mais_le_dit(self):
        """Zero table isolee n'est pas une faute : c'est un fait a rapporter.

        Le plan reste inactif — rien a exporter differemment — mais
        `standalone_requested` distingue « regle posee, rien trouve » de
        « regle absente », que le rapport ne doit pas confondre.
        """
        plan = plan_table_filter(
            _faux(fk_sortantes=_TABLES), "SRC", standalone=True
        )
        self.assertFalse(plan.active)
        self.assertEqual(plan.standalone, [])
        self.assertEqual(plan.data_only, [])
        self.assertTrue(plan.standalone_requested)

    def test_la_definition_est_dans_la_requete(self):
        """Les conditions de la definition sont bien envoyees.

        Le faux repond a partir de declarations ; rien dans la reponse ne
        prouve que la **requete** porte les deux `NOT EXISTS`. C'est
        pourtant toute la definition referentielle : une requete qui
        oublierait le volet entrant exclurait des tables que d'autres
        schemas attendent. Le troisieme volet — l'usage — est lu a part,
        et doit lui aussi atteindre la base.
        """
        faux = _faux(
            dependants={**_DEPENDANTS, "SAL_HISTORY": ["TRG_SAL"]},
            signatures=_SIGNATURES + [["TRIGGER", "TRG_SAL"]],
        )
        plan_table_filter(faux, "SRC", standalone=True)
        sql = " ".join(faux.queries).lower()
        self.assertEqual(sql.count("not exists"), 2)
        self.assertIn("r_constraint_name", sql)
        self.assertIn("constraint_type = 'r'", sql)
        self.assertIn("from dba_tables", sql)
        self.assertIn("from dba_dependencies", sql)
        self.assertIn("referenced_type = 'table'", sql)
        # La base des triggers est relue pour ne pas confondre le trigger
        # propre a la table avec un lecteur externe.
        self.assertIn("select trigger_name, table_name from dba_triggers", sql)

    def test_l_exclusion_totale_prime_sur_la_regle_isolee(self):
        """Une table des deux listes part en entier, et n'est plus « isolee ».

        La nommer encore parmi les isolees ferait lire deux traitements
        pour une seule table — dont l'un, le mode B, n'a pas eu lieu.
        """
        plan = plan_table_filter(
            _faux(), "SRC", tables=["REGIONS"], standalone=True
        )
        self.assertIn("REGIONS", plan.overlap)
        self.assertIn("REGIONS", plan.excluded)
        self.assertNotIn("REGIONS", plan.data_only)
        self.assertNotIn("REGIONS", plan.standalone)

    def test_la_regle_emet_des_query_et_jamais_d_exclude(self):
        """La regle est du mode B : des `query=`, aucun `exclude=`.

        Un `exclude=` ici contredirait `INCLUDE`, que le mode B accepte ;
        c'est le controle qui garde la regle dans son mode.
        """
        faux = _faux(fk_sortantes=["EMPLOYEES"])
        plan = plan_table_filter(faux, "SRC", standalone=True)
        self.assertEqual(plan.exclude_clause(), "")
        self.assertEqual(len(plan.query_lines()), 4)
        for ligne in plan.query_lines():
            self.assertIn('"WHERE 1=2"', ligne)

    def test_un_plan_avec_isolees_survit_a_l_etat(self):
        """Resoudre a l'etape 6, figer, relire : le `resume` doit voir
        la meme liste, drapeau compris."""
        plan = plan_table_filter(_faux(), "SRC", standalone=True)
        relu = TableFilterPlan.from_dict(plan.to_dict())
        self.assertEqual(relu.standalone, plan.standalone)
        self.assertTrue(relu.standalone_requested)
        self.assertEqual(relu.to_dict(), plan.to_dict())

    def test_un_etat_anterieur_ignore_le_drapeau(self):
        """Un etat ecrit avant la regle se relit avec le drapeau a `False`."""
        plan = TableFilterPlan.from_dict({"schema": "SRC"})
        self.assertEqual(plan.standalone, [])
        self.assertFalse(plan.standalone_requested)


class TestAdditionDesOptions(unittest.TestCase):
    """`EXCLUDE_STANDALONE_TABLES` s'ajoute aux autres, il ne les remplace pas.

    Les quatre listes ecrites et la regle des isolees vivent dans deux
    modes : `EXCLUDE_TABLES` et son motif sont des exclusions **totales**,
    `EXCLUDE_TABLES_DATA` et son motif des exclusions **de donnees**. La
    regle verse dans le second. Rien dans le code ne doit court-circuiter
    l'un au profit de l'autre : desactiver les listes quand la regle est
    posee ferait disparaitre un filtre que l'exploitant a demande.
    """

    #: Les isolees du faux schema par defaut (`EMPLOYEES` est lue par
    #: `V_EMP` et `PKG_EMP`, donc pas isolee).
    _ISOLEES = ["DEPARTMENTS", "REGIONS", "SAL_HISTORY", "TMP_LOAD"]

    def test_exclude_tables_s_ajoute_a_la_regle_isolee(self):
        """Mode A ecrit **et** regle posee : les deux effets coexistent.

        Sans addition, poser la regle ferait oublier `EXCLUDE_TABLES` — ou
        l'inverse — et la table pourtant nommee partirait.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables=["EMPLOYEES"], standalone=True)
        self.assertEqual(plan.excluded, ["EMPLOYEES"])
        self.assertEqual(plan.data_only, self._ISOLEES)
        self.assertEqual(plan.standalone, self._ISOLEES)
        # Les deux formes atteignent le parfile en meme temps.
        self.assertEqual(plan.exclude_clause(), 'TABLE:"IN (\'EMPLOYEES\')"')
        self.assertEqual(len(plan.query_lines()), len(self._ISOLEES))

    def test_exclude_tables_data_s_ajoute_a_la_regle_isolee(self):
        """Deux sources du mode B : la liste ecrite **et** la regle.

        `EMPLOYEES` n'est pas isolee, mais elle est explicitement demandee
        sans donnees : elle doit y etre, a cote des isolees. La regle ne
        la remplace pas, et elle ne la retire pas non plus.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables_data=["EMPLOYEES"], standalone=True)
        self.assertEqual(
            plan.data_only,
            ["DEPARTMENTS", "EMPLOYEES", "REGIONS", "SAL_HISTORY", "TMP_LOAD"],
        )
        # `standalone` ne liste que les isolees : `EMPLOYEES` est en mode B
        # par la liste, pas par la regle.
        self.assertEqual(plan.standalone, self._ISOLEES)

    def test_exclude_tables_regex_s_ajoute_a_la_regle_isolee(self):
        """Le motif du mode A compte comme la liste : il reste actif."""
        plan = plan_table_filter(_faux(), "SRC",
                                 tables_regex=["^EMP"], standalone=True)
        self.assertEqual(plan.excluded, ["EMPLOYEES"])
        self.assertEqual(plan.data_only, self._ISOLEES)
        self.assertEqual(plan.standalone, self._ISOLEES)

    def test_exclude_tables_data_regex_s_ajoute_a_la_regle_isolee(self):
        """Le motif du mode B s'unit aux isolees, sans en ecraser aucune."""
        plan = plan_table_filter(_faux(), "SRC",
                                 tables_data_regex=["^EMP"], standalone=True)
        self.assertIn("EMPLOYEES", plan.data_only)
        self.assertEqual(
            plan.data_only,
            ["DEPARTMENTS", "EMPLOYEES", "REGIONS", "SAL_HISTORY", "TMP_LOAD"],
        )
        self.assertEqual(plan.standalone, self._ISOLEES)

    def test_le_chevauchement_ne_retire_que_la_table_concernee(self):
        """Une isolee nommee en exclusion totale part en entier ; les
        autres isolees restent en mode B.

        C'est le seul point ou les deux modes se rencontrent, et il ne doit
        emporter que la table en cause — pas toute la regle.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables=["REGIONS"], standalone=True)
        self.assertEqual(plan.excluded, ["REGIONS"])
        self.assertEqual(plan.overlap, ["REGIONS"])
        self.assertNotIn("REGIONS", plan.data_only)
        self.assertNotIn("REGIONS", plan.standalone)
        self.assertEqual(
            plan.data_only, ["DEPARTMENTS", "SAL_HISTORY", "TMP_LOAD"]
        )
        self.assertEqual(
            plan.standalone, ["DEPARTMENTS", "SAL_HISTORY", "TMP_LOAD"]
        )


class TestEtatCongele(unittest.TestCase):
    """La representation rangee dans l'etat, et sa relecture par un
    `resume`."""

    def test_le_plan_survit_un_tour_par_l_etat(self):
        """Un `resume` doit rejouer la meme liste que l'export qu'il rejoue.

        Le plan est reconstruit depuis le dictionnaire fige, jamais
        recalcule : le schema a pu changer entre les deux, et un dump
        exporte avec une liste differente de celle que l'etat annonce
        serait un autre run, presente comme la suite de celui-ci.
        """
        plan = plan_table_filter(_faux(), "SRC",
                                 tables=["EMPLOYEES"],
                                 tables_data=["SAL_HISTORY"])
        relu = TableFilterPlan.from_dict(plan.to_dict())
        self.assertEqual(relu.to_dict(), plan.to_dict())
        self.assertEqual(relu.excluded, ["EMPLOYEES"])
        self.assertEqual(relu.data_only, ["SAL_HISTORY"])

    def test_un_etat_anterieur_sans_filtre_se_relit(self):
        """Une version anterieure ne doit pas rendre la reprise impossible.

        Les cles manquantes restent vides : refuser de relire un etat
        ancien couperait la reprise d'un run qui, lui, n'avait aucun
        filtre — pour une faute qu'il n'a pas commise.
        """
        for brut in ({}, {"schema": "SRC"}):
            with self.subTest(brut=brut):
                plan = TableFilterPlan.from_dict(brut)
                self.assertFalse(plan.active)
                self.assertEqual(plan.ignored, [])

    def test_signature_et_nom_forment_une_paire(self):
        """Le format est celui de l'etape 16, tilde compris.

        `split(..., 1)` : un nom d'objet quote peut lui-meme contenir un
        tilde, et il faut le retrouver entier — un decoupage naif
        produirait un nom tronque, donc un objet « different » a la
        comparaison.
        """
        self.assertEqual(signature("TABLE ", " EMPLOYEES"),
                         "TABLE~EMPLOYEES")
        self.assertEqual(nom_de_signature("TABLE~FOO~BAR"), "FOO~BAR")


if __name__ == "__main__":
    unittest.main()
