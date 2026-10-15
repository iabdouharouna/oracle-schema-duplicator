"""Filtrage des tables dont les donnees ne partent pas vers la cible.

Deux modes, et ils ne disent pas la meme chose. La distinction est celle
de l'intention de l'exploitant, pas du resultat affiche :

* `EXCLUDE_TABLES` (mode A) — la table ne part **pas du tout**. Ni DDL ni
  donnees. Data Pump la saute par `EXCLUDE=TABLE:"IN (...)"`, et la 19c
  emporte avec elle, en cascade, ses index, contraintes, grants et
  triggers. Les objets qui **dependent** de la table (vues, procedures,
  packages, triggers d'autres tables) sont retires egalement : creer une
  vue sur une table absente rend `ORA-00942` a l'import, et une
  procedure ne se compile pas sans les tables qu'elle reference. Leur
  retrait est annonce a l'etape 6, jamais silencieux.

* `EXCLUDE_TABLES_DATA` (mode B) — la structure part, les lignes non.
  Une valeur `query=<schema>.<table>:"WHERE 1=2"` par table (le prefixe
  `query=` etant pose par l'adaptateur Data Pump), posee a
  l'**export seul** : le dump ne contient deja plus les lignes, et poser
  `QUERY` a l'import rejeterait des lignes que rien ne demande a
  rejeter.

Trois faits de la 19c, verifies sur la base reelle et non supposes,
encadrent la construction du parfile :

1. `exclude=USER;TABLE:"..."` est refuse (`ORA-39038`, type d'objet
   inexistant) : le separateur `;` n'existe pas. Chaque filtre a sa
   propre ligne `exclude=`.
2. `INCLUDE` et `EXCLUDE` sont incompatibles (`UDE-00011`) : la
   combinaison est refusee au chargement de la configuration.
3. `QUERY` se cumule sans probleme a `EXCLUDE`, a `INCLUDE` et a
   `PARALLEL>1` — verifie avec un export `REGIONS` a 0 ligne en
   parallel 4.

La resolution a lieu a l'etape 6, contre le schema source reel, et le
plan est fige dans l'etat : un `resume` rejoue l'export qu'il a deja
commence, avec la meme liste, meme si le schema a change entre temps.
Reprendre un export avec une liste differente de celle de l'export
serait rejouer un autre run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Set

from ..errors import ConfigError

#: Nom d'objet simple : ecrit sans quote dans un parfile. Toute autre
#: forme (espace, casse manglee, caractere special) est quotee, et un
#: guillemet interne est double — la meme regle que `_q`.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")

#: Types d'objets que l'inventaire n'a jamais retenus. Filtre **identique**
#: a `Pipeline._inventory` : l'etape 16 et ce module doivent parler la
#: meme langue, sinon un synonyme retire par l'un et conserve par l'autre
#: devient un ecart d'inventaire inexplicable.
_NON_INVENTORIAITABLES = "object_type not in ('SYNONYM', 'DATABASE LINK')"


def signature(object_type: str, object_name: str) -> str:
    """Signature d'objet au format de l'etape 16 : `TYPE~NOM`."""
    return f"{object_type.strip()}~{object_name.strip()}"


def nom_de_signature(sig: str) -> str:
    """Nom d'objet porte par une signature.

    `split(..., 1)` et non `split("~")` : un nom d'objet quote peut
    lui-meme contenir un tilde, et il faut alors le retrouver entier.
    """
    return sig.split("~", 1)[1]


def _lit(value: str) -> str:
    """Echappe un litteral SQL : les noms de table viennent de la base."""
    return str(value).replace("'", "''")


def _in(noms: Sequence[str]) -> str:
    return ", ".join("'" + _lit(n) + "'" for n in sorted(set(noms)))


def _ident(nom: str) -> str:
    """Nom d'objet pour une valeur de parfile non quotee globalement."""
    if _IDENT_RE.match(nom):
        return nom
    return '"' + nom.replace('"', '""') + '"'


def _motifs(entrees: Sequence[str], cle: str) -> List["re.Pattern[str]"]:
    """Compile les expressions regulieres d'une cle.

    La configuration valide deja ces motifs au chargement. Cette seconde
    compilation est un filet : si elle echoue, le refus doit porter le
    meme nom de cle que le refus au chargement, sinon l'exploitant lit
    deux diagnostics differents pour la meme faute.
    """
    motifs: List["re.Pattern[str]"] = []
    for brut in entrees:
        texte = str(brut)
        if not texte.strip():
            continue
        try:
            motifs.append(re.compile(texte, re.IGNORECASE))
        except re.error as exc:
            raise ConfigError(
                f"{cle} : expression reguliere invalide {texte!r} ({exc.msg})",
                hint="Syntaxe `re` Python : alternance `|`, bornes {n,m}, "
                     "ancres `^` et `$`. Le motif est refuse au chargement "
                     "de la configuration, avant toute connexion.",
            ) from None
    return motifs


# --------------------------------------------------------------------------
# Interrogations du schema
# --------------------------------------------------------------------------

def _table_names(adapter, schema: str) -> List[str]:
    """Tables du schema, telles que Data Pump les voit.

    `DBA_OBJECTS.OBJECT_TYPE = 'TABLE'` et non `DBA_TABLES` : c'est
    exactement l'objet que filtre `EXCLUDE=TABLE`. Les partitions ont
    leur propre type, les vues egalement — un nom qui ne serait pas un
    objet `TABLE` ne peut pas etre filtre, et le pretendantre le serait
    un filtre sans effet, signale comme actif.
    """
    rows = adapter.query(
        "select object_name from dba_objects "
        f"where owner = '{_lit(schema)}' and object_type = 'TABLE' "
        "and object_name not like 'BIN$%' order by object_name"
    )
    return [str(row[0]).strip() for row in rows if row]


def _owned_names(adapter, schema: str, tables: Sequence[str]) -> Set[str]:
    """Objets qui partent **avec** ces tables.

    La cascade est un fait observe, pas une hypothese : `EXCLUDE=TABLE`
    emporte index, contraintes, grants, triggers et LOB. Les noms sont
    recoltes ici pour deux usages differents — retirer ces objets de
    l'inventaire de l'etape 16, et retirer leurs segments de l'estimation
    d'espace de l'etape 9. Un oubli se paie en echec de comparaison, un
    exces se paie en estimation pessimisee ; les deux restent lisibles.
    """
    if not tables:
        return set()
    noms = _in(tables)
    rows = adapter.query(
        "select object_name from dba_objects "
        f"where owner = '{_lit(schema)}' and {_NON_INVENTORIAITABLES} "
        f"and (object_name in ({noms}) "
        f"or object_name in (select index_name from dba_indexes "
        f"                   where owner = '{_lit(schema)}' "
        f"                   and table_name in ({noms})) "
        f"or object_name in (select trigger_name from dba_triggers "
        f"                   where owner = '{_lit(schema)}' "
        f"                   and table_name in ({noms})) "
        f"or object_name in (select segment_name from dba_lobs "
        f"                   where owner = '{_lit(schema)}' "
        f"                   and table_name in ({noms})) "
        f"or object_name in (select index_name from dba_lobs "
        f"                   where owner = '{_lit(schema)}' "
        f"                   and table_name in ({noms})))"
    )
    return {str(row[0]).strip() for row in rows if row}


def _dependent_names(adapter, schema: str, tables: Sequence[str]) -> Set[str]:
    """Objets qui dependent de ces tables, directement puis transitivement.

    Le point fixe est necessaire, pas un luxe : une vue depend de la
    table, une procedure depend de la vue, et creer les trois a l'import
    echoue sur la troisieme. La boucle s'arrete d'elle-meme, l'ensemble
    des noms croissant et le schema etant fini.

    Seuls les objets du **schema exporte** sont retenus : une vue d'un
    autre schema qui pointe sur nos tables n'est pas dupliquee ici, et
    n'est donc pas dupliquee avec elle.
    """
    if not tables:
        return set()
    vus: Set[str] = {str(t) for t in tables}
    a_visiter: Set[str] = set(vus)
    trouves: Set[str] = set()
    while a_visiter:
        rows = adapter.query(
            "select distinct name from dba_dependencies "
            f"where owner = '{_lit(schema)}' "
            f"and referenced_owner = '{_lit(schema)}' "
            f"and referenced_name in ({_in(a_visiter)})"
        )
        nouveaux = {str(row[0]).strip() for row in rows if row} - vus
        if not nouveaux:
            break
        vus |= nouveaux
        trouves |= nouveaux
        a_visiter = nouveaux
    return trouves


def _signatures(adapter, schema: str, noms: Sequence[str]) -> List[str]:
    """Signatures `TYPE~NOM` des objets donnes, au format de l'etape 16.

    Les noms sont **revalides** contre `DBA_OBJECTS` plutot que combines
    avec un type deduit : un nom peut porter plusieurs objets (un package
    et son corps), et un type suppose devient un ecart d'inventaire
    qu'aucun message n'expliquerait.
    """
    if not noms:
        return []
    rows = adapter.query(
        "select object_type, object_name from dba_objects "
        f"where owner = '{_lit(schema)}' and {_NON_INVENTORIAITABLES} "
        f"and object_name in ({_in(noms)}) "
        "order by object_type, object_name"
    )
    return [signature(str(r[0]), str(r[1])) for r in rows if len(r) > 1]


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

@dataclass
class TableFilterPlan:
    """Filtrage resolu, tel qu'il sera applique jusqu'a la fin du run."""

    schema: str = ""
    #: Tables hors duplication (mode A).
    excluded: List[str] = field(default_factory=list)
    #: Tables dupliquees sans leurs lignes (mode B).
    data_only: List[str] = field(default_factory=list)
    #: Objets retires uniquement parce qu'ils dependent d'une table
    #: filtrees (vues, procedures, packages...). Annonces, pas silencieux.
    dependents: List[str] = field(default_factory=list)
    #: Signatures retirees de l'inventaire source a l'etape 16.
    ignored: List[str] = field(default_factory=list)
    #: Signatures dont les segments ne voyagent pas (les deux modes) :
    #: base de l'estimation d'espace de l'etape 9.
    segment_ignored: List[str] = field(default_factory=list)
    #: Entrees de configuration sans correspondance, sous la forme
    #: `CLE=entree`. Un avertissement, jamais un echec.
    unmatched: List[str] = field(default_factory=list)
    #: Tables demandees dans les deux listes : l'exclusion totale prime.
    overlap: List[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """Un filtre est-il configure ?"""
        return bool(self.excluded or self.data_only)

    def exclude_clause(self) -> str:
        """Clause `TABLE:"IN (...)"` pour un parfile, ou chaine vide.

        Une seule clause couvre toutes les tables filtrees : chaque
        filtre a sa ligne `exclude=`, et la liste interne est separee par
        des virgules, qui sont la syntaxe SQL de `IN` — pas par un
        separateur de filtres, qui n'existe pas en 19c.
        """
        if not self.excluded:
            return ""
        noms = ", ".join(
            "'" + _lit(n).replace('"', '""') + "'" for n in self.excluded
        )
        return f'TABLE:"IN ({noms})"'

    def query_lines(self) -> List[str]:
        """Valeurs `QUERY` de l'export, une par table du mode B.

        La valeur est **nue** : le prefixe `query=` est pose par
        l'adaptateur Data Pump, comme pour `exclude` et `include`. Le
        poser ici le dupliquerait en `query=query=...`, refuse par
        Data Pump (`LRM-00116: syntax error at 'query' following '='`).

        La clause `WHERE` est entre guillemets **doubles**, la forme
        documentee par Oracle pour `QUERY` ; les guillemets simples sont
        reserves au cas ou la clause contiendrait elle-meme un `"`.
        """
        return [
            f'{self.schema.upper()}.{_ident(t)}:"WHERE 1=2"'
            for t in self.data_only
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Representation figee, rangee dans l'etat du run."""
        return {
            "schema": self.schema,
            "excluded": list(self.excluded),
            "data_only": list(self.data_only),
            "dependents": list(self.dependents),
            "ignored": list(self.ignored),
            "segment_ignored": list(self.segment_ignored),
            "unmatched": list(self.unmatched),
            "overlap": list(self.overlap),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TableFilterPlan":
        """Reconstruit un plan fige par un `resume`.

        Les cles manquantes restent vides : un etat produit par une
        version anterieure doit se relire sans erreur, pas empecher la
        reprise d'un run dont le filtre etait plus simple.
        """
        def liste(cle: str) -> List[str]:
            return [str(v) for v in data.get(cle) or []]

        return cls(
            schema=str(data.get("schema", "")),
            excluded=liste("excluded"),
            data_only=liste("data_only"),
            dependents=liste("dependents"),
            ignored=liste("ignored"),
            segment_ignored=liste("segment_ignored"),
            unmatched=liste("unmatched"),
            overlap=liste("overlap"),
        )


def plan_table_filter(
    adapter,
    schema: str,
    *,
    tables: Sequence[str] = (),
    tables_regex: Sequence[str] = (),
    tables_data: Sequence[str] = (),
    tables_data_regex: Sequence[str] = (),
) -> TableFilterPlan:
    """Resout les quatre cles contre le schema source reel.

    Trois sorties sont possibles, et aucune n'est un echec :

    * une entree exacte introuvable ou un motif sans correspondance —
      `unmatched`, qui devient un avertissement. La table peut avoir
      change de nom depuis le dernier run ; refuser le run sur ce
      signal, ce serait rendre un changement de nom inobservable ;
    * une table demandee dans les deux listes — `overlap`, avertissement
      egalement, et l'exclusion totale prime ;
    * une regex mal formee — `ConfigError` code 1, refusee au chargement
      de la configuration bien avant cette fonction.
    """
    plan = TableFilterPlan(schema=schema)
    if not (tables or tables_regex or tables_data or tables_data_regex):
        # Aucun filtre : aucune requete, aucun etat a figer. Un run sans
        # filtre doit se comporter exactement comme avant cette
        # fonctionnalite.
        return plan

    existantes = _table_names(adapter, schema)
    par_nom = {n.upper(): n for n in existantes}
    mode_a: Set[str] = set()
    mode_b: Set[str] = set()
    sans_objet: List[str] = []

    for cle, entrees, cible in (
        ("EXCLUDE_TABLES", tables, mode_a),
        ("EXCLUDE_TABLES_DATA", tables_data, mode_b),
    ):
        for brut in entrees:
            entree = str(brut).strip()
            if not entree:
                continue
            trouve = par_nom.get(entree.upper())
            if trouve is None:
                sans_objet.append(f"{cle}={entree}")
            else:
                cible.add(trouve)

    for cle, motifs, cible in (
        ("EXCLUDE_TABLES_REGEX", _motifs(tables_regex, "EXCLUDE_TABLES_REGEX"),
         mode_a),
        ("EXCLUDE_TABLES_DATA_REGEX",
         _motifs(tables_data_regex, "EXCLUDE_TABLES_DATA_REGEX"), mode_b),
    ):
        for motif in motifs:
            touches = [n for n in existantes if motif.search(n)]
            if not touches:
                sans_objet.append(f"{cle}={motif.pattern}")
            cible.update(touches)

    plan.excluded = sorted(mode_a)
    plan.data_only = sorted(mode_b)
    plan.unmatched = sans_objet
    plan.overlap = sorted(mode_a & mode_b)
    if plan.overlap:
        # Une table demandee vide **et** hors duplication n'est pas les
        # deux a la fois : le filtre le plus fort gagne, et l'autre
        # disparait plutot que de produire deux lignes contradictoires
        # dans le parfile.
        plan.data_only = [t for t in plan.data_only if t not in mode_a]

    if not plan.active:
        return plan

    possedes_a = (
        _owned_names(adapter, schema, plan.excluded) if plan.excluded else set()
    )
    possedes_b = (
        _owned_names(adapter, schema, plan.data_only) if plan.data_only else set()
    )
    dependants = (
        _dependent_names(adapter, schema, plan.excluded) if plan.excluded else set()
    )
    sigs = _signatures(adapter, schema, possedes_a | possedes_b | dependants)

    noms_inventaire = possedes_a | dependants
    noms_segments = possedes_a | possedes_b
    plan.ignored = [s for s in sigs if nom_de_signature(s) in noms_inventaire]
    plan.segment_ignored = [
        s for s in sigs if nom_de_signature(s) in noms_segments
    ]
    plan.dependents = [
        s for s in plan.ignored if nom_de_signature(s) in dependants
    ]
    return plan
