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

* `EXCLUDE_STANDALONE_TABLES` — pas un troisieme mode, une **regle** qui
  alimente le mode B. Elle y verse les tables **isolees et inutilisees**
  du schema source, celles qui satisfont **deux** conditions :

  1. aucune FK ne la relie : ni sortante (la table ne reference rien),
     ni entrante (aucune contrainte, d'aucun schema, ne la reference) ;
  2. aucun objet **autre qu'une table** ne la reference : vue, procedure,
     fonction, package, type, synonyme, ou trigger **d'une autre table**.
     Le trigger pose sur la table elle-meme ne compte pas : il fait partie
     de son DDL et voyage avec elle.

  Le seul isolement referentiel ne suffit pas a dire une table morte :
  une table sans FK peut tres bien etre lue par un package, et retirer
  ses lignes viderait une donnee attendue. C'est le second volet qui
  etablit l'inutilisation. Leur structure part, leurs lignes non. Une
  table isolee n'ayant par construction aucune relation referentielle,
  retirer ses lignes ne peut pas rendre une FK invalide : la consequence
  `ORA-02298` du mode B ne s'applique jamais a cette regle. Elle est
  opt-in : un schema ou l'application gere ses relations hors
  contraintes se couvrirait entierement, et ce n'est pas a l'outil de le
  deduire.

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


def _standalone_tables(adapter, schema: str) -> List[str]:
    """Tables du schema qu'aucune FK ne relie et que nul objet n'utilise.

    Une table est isolee quand **les trois** conditions tiennent :

    1. elle n'a aucune FK **sortante** — aucune contrainte ``R`` portee
       a son nom ;
    2. aucune FK, **d'aucun schema**, ne la reference — nul
       ``r_constraint_name`` ne pointe sur une de ses contraintes ;
    3. aucun objet autre qu'une table ne la reference (cf.
       `_used_tables`) : vue, procedure, fonction, package, type,
       synonyme, ou trigger **d'une autre table**. Le trigger pose sur la
       table elle-meme n'est pas un lecteur : il fait partie de son DDL.

    Les deux premiers volets sont dans une seule requete : ce sont les
    deux moities d'une meme definition, et les separer en deux appels
    laisserait la porte ouverte a un schema lu differemment entre les
    deux — une table isolee par le premier et liee par le second.

    Le second volet ne restreint **pas** `c.owner` : une FK d'un autre
    schema vers cette table la rend liee au meme titre qu'une FK
    interne. L'export ne transporte que le schema demande, mais la table
    n'est plus « isolee » pour autant — c'est la definition, et
    l'elargir ferait exclure des donnees qu'un autre schema attend.

    Le troisieme volet est lu **a part** (`_used_tables`), pour la meme
    raison de cout que les autres lectures completes du module : le volet
    des dependances en `NOT EXISTS` correle a ete mesure a 32 s sur le
    schema reel, la lecture complete suivie d'un recoupement Python a
    12 s — 15 s quand une lecture de `DBA_TRIGGERS` s'y ajoute — meme
    resultat. L'isolement referentiel ne suffit pas a declarer une table
    morte : une table sans FK peut etre lue par un package, et retirer
    ses lignes viderait une donnee attendue. Le trigger pose sur la table
    elle-meme, en revanche, ne la disqualifie pas : il part avec son DDL.

    `DBA_TABLES` et non `DBA_OBJECTS` : `DBA_CONSTRAINTS` nomme les
    tables, pas les objets, et c'est ce que la definition interroge. La
    liste est ensuite recoupee avec les tables que l'export voit
    reellement (cf. `plan_table_filter`), ce qui ecarte tout nom qui ne
    serait pas une table du dump.
    """
    rows = adapter.query(
        "select t.table_name from dba_tables t "
        f"where t.owner = '{_lit(schema)}' "
        "and not exists (select 1 from dba_constraints c "
        "where c.owner = t.owner and c.table_name = t.table_name "
        "and c.constraint_type = 'R') "
        "and not exists (select 1 from dba_constraints p "
        "join dba_constraints c "
        "on c.r_owner = p.owner "
        "and c.r_constraint_name = p.constraint_name "
        "where p.owner = t.owner and p.table_name = t.table_name "
        "and c.constraint_type = 'R') "
        "order by t.table_name"
    )
    isolees = {str(row[0]).strip() for row in rows if row}
    return sorted(isolees - _used_tables(adapter, schema))


def _used_tables(adapter, schema: str) -> Set[str]:
    """Tables qu'un objet **autre qu'une table** lit reellement.

    `DBA_DEPENDENCIES` porte les dependances directes entre objets du
    dictionnaire : une vue, une procedure, une fonction, un package, un
    type ou un synonyme qui lit une table y figure avec
    `referenced_type = 'TABLE'` et son propre `type`. Un trigger y figure
    aussi, sous `type = 'TRIGGER'` — mais tous les triggers ne comptent
    pas de la meme facon.

    Un trigger **pose sur la table elle-meme** fait partie de son DDL et
    voyage avec elle : il ne la rend pas « utilisee ». Seul un trigger
    **d'une autre table**, dont le corps lit celle-ci, la disqualifie.
    `DBA_TRIGGERS` donne la table de base de chaque trigger, et c'est
    cette correspondance qui separe les deux cas. Les tables referencees
    par une **autre table** sont ecartees de meme : c'est le lien FK,
    deja traite par le volet referentiel de `_standalone_tables`.

    Aucun filtre `owner` sur le referenceur : un objet d'un autre schema
    qui lit la table la rend utilisee au meme titre qu'un objet interne —
    la meme symetrie que le volet FK entrant. Les deux vues sont lues
    **en entier** pour le schema, sans liste, et le recoupement se fait
    ici, en Python, comme partout ailleurs dans ce module. La lecture de
    `DBA_TRIGGERS` est **conditionnee** a la presence d'au moins un
    trigger dans les dependances : un schema sans trigger ne la paie pas.
    Mesure sur le schema reel : 8,2 s pour 51 065 aretes, 3,5 s pour
    7 673 triggers.

    La dependance est lue **directe**, et cela suffit : toute chaine
    d'utilisation passe par un dependant direct de la table — une vue lue
    par un package est elle-meme dependante de la table — donc l'absence
    de dependant direct est bien l'absence d'utilisation.

    Limite connue : `DBA_DEPENDENCIES` ne connait que les references
    **statiques**. Un package qui nomme la table par SQL dynamique
    (`EXECUTE IMMEDIATE`) n'y figure pas, et l'outil ne lit pas le code
    source pour le deviner. La table serait alors classee isolee a tort,
    ses lignes non copiees. Un schema ou l'application lit des tables de
    donnees en SQL dynamique doit laisser `EXCLUDE_STANDALONE_TABLES` a
    `false`.
    """
    rows = adapter.query(
        "select referenced_name, name, type from dba_dependencies "
        f"where referenced_owner = '{_lit(schema)}' "
        "and referenced_type = 'TABLE'"
    )
    lignes = [row for row in rows if len(row) > 2]
    base: Dict[str, str] = {}
    if any(str(row[2]).strip() == "TRIGGER" for row in lignes):
        base = {
            str(row[0]).strip(): str(row[1]).strip()
            for row in adapter.query(
                "select trigger_name, table_name from dba_triggers "
                f"where owner = '{_lit(schema)}'")
            if len(row) > 1
        }
    utilises: Set[str] = set()
    for row in lignes:
        reference = str(row[0]).strip()
        nom = str(row[1]).strip()
        typ = str(row[2]).strip()
        if typ == "TABLE":
            continue
        if typ == "TRIGGER" and base.get(nom) == reference:
            continue
        utilises.add(reference)
    return utilises


def _owned_names(adapter, schema: str, tables: Sequence[str]) -> Set[str]:
    """Objets qui partent **avec** ces tables.

    La cascade est un fait observe, pas une hypothese : `EXCLUDE=TABLE`
    emporte index, contraintes, grants, triggers et LOB. Les noms sont
    recoltes ici pour deux usages differents — retirer ces objets de
    l'inventaire de l'etape 16, et retirer leurs segments de l'estimation
    d'espace de l'etape 9. Un oubli se paie en echec de comparaison, un
    exces se paie en estimation pessimisee ; les deux restent lisibles.

    Les vues de dictionnaire sont lues **en entier** pour le schema, et
    le recoupement se fait ici, en Python. La liste des tables filtrees
    ne figure donc dans aucune requete. Ce n'est pas une preference : une
    requete qui la portait cinq fois (`object_name in (...)` plus les
    quatre sous-requetes de cascade) a ete mesuree a plus de 600 s de
    delai depasse sur le schema reel des que la liste approchait les
    1000 expressions — le plan d'execution s'effondre bien avant que le
    shell distant ne bronche. La lecture complete, elle, coute 6,7 s pour
    12 464 lignes sur ce meme schema.
    """
    if not tables:
        return set()
    designees = {str(t) for t in tables}
    noms: Set[str] = set(designees)
    rows = adapter.query(
        "select table_name, index_name from dba_indexes "
        f"where owner = '{_lit(schema)}' "
        "union all select table_name, trigger_name from dba_triggers "
        f"where owner = '{_lit(schema)}' "
        "union all select table_name, segment_name from dba_lobs "
        f"where owner = '{_lit(schema)}' "
        "union all select table_name, index_name from dba_lobs "
        f"where owner = '{_lit(schema)}'"
    )
    for row in rows:
        if len(row) > 1 and str(row[0]).strip() in designees:
            noms.add(str(row[1]).strip())
    return noms


def _dependent_names(adapter, schema: str, tables: Sequence[str]) -> Set[str]:
    """Objets qui dependent de ces tables, directement puis transitivement.

    Le point fixe est necessaire, pas un luxe : une vue depend de la
    table, une procedure depend de la vue, et creer les trois a l'import
    echoue sur la troisieme. La boucle s'arrete d'elle-meme, l'ensemble
    des noms croissant et le schema etant fini.

    Seuls les objets du **schema exporte** sont retenus : une vue d'un
    autre schema qui pointe sur nos tables n'est pas dupliquee ici, et
    n'est donc pas dupliquee avec elle.

    Le graphe est lu **une fois**, en entier, puis parcouru en Python :
    une lecture par niveau aurait ramene la liste du front dans une
    clause `IN`, donc les deux plafonds evites ailleurs — 1000
    expressions cote Oracle, et une ligne de script trop longue cote
    shell. Ce chemin n'est emprunte que par le mode A (`EXCLUDE_TABLES`) ;
    13 s pour 98 385 aretes sur le schema reel, et rien du tout quand le
    mode A est vide, ce qui est le cas de la regle des tables isolees.
    """
    if not tables:
        return set()
    rows = adapter.query(
        "select referenced_name, name from dba_dependencies "
        f"where owner = '{_lit(schema)}' "
        f"and referenced_owner = '{_lit(schema)}'"
    )
    referenceurs: Dict[str, Set[str]] = {}
    for row in rows:
        if len(row) > 1:
            referenceurs.setdefault(str(row[0]).strip(), set()).add(
                str(row[1]).strip()
            )
    vus: Set[str] = {str(t) for t in tables}
    a_visiter: Set[str] = set(vus)
    trouves: Set[str] = set()
    while a_visiter:
        suivants: Set[str] = set()
        for nom in a_visiter:
            suivants |= referenceurs.get(nom, set())
        nouveaux = suivants - vus
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
    voulus = {str(n) for n in noms}
    rows = adapter.query(
        "select object_type, object_name from dba_objects "
        f"where owner = '{_lit(schema)}' and {_NON_INVENTORIAITABLES} "
        "order by object_type, object_name"
    )
    return [signature(str(r[0]), str(r[1])) for r in rows
            if len(r) > 1 and str(r[1]).strip() in voulus]


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
    #: Tables isolees ajoutees au mode B par `EXCLUDE_STANDALONE_TABLES` :
    #: celles qu'aucune FK ne relie et que nul autre objet ne reference
    #: (cf. `_standalone_tables`). Sous-ensemble de `data_only` — une
    #: table que le mode A retire en entier n'y figure pas, sans quoi le
    #: rapport nommerait une donnee qui vient de partir autrement.
    standalone: List[str] = field(default_factory=list)
    #: La cle `EXCLUDE_STANDALONE_TABLES` a-t-elle ete posee ? Distinct de
    #: `standalone` (la liste) : une regle activee sur un schema sans
    #: table isolee ne filtre rien, mais doit le dire. Sans ce drapeau,
    #: « regle appliquee, rien trouve » et « regle absente » auraient le
    #: meme rapport — un silence.
    standalone_requested: bool = False

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

        Limite connue, et volontairement pas contournee ici : la clause
        tient sur **une** ligne de parfile, donc du script distant. Une
        liste de plusieurs milliers de tables depasserait a la fois les
        1000 expressions d'un `IN` (`ORA-01795`) et la longueur de ligne
        que le shell survit. Le mode A est une liste explicite, courte en
        pratique ; le mode B, lui, emet une `query=` par table et n'a pas
        ce defaut. Une liste de masse demanderait une `exclude=` par
        table, ce que Data Pump accepte mais dont l'accumulation n'a pas
        ete verifiee ici — on ne l'ecrit donc pas a l'aveugle.
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
            "standalone": list(self.standalone),
            "standalone_requested": bool(self.standalone_requested),
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
            standalone=liste("standalone"),
            standalone_requested=bool(data.get("standalone_requested", False)),
        )


def plan_table_filter(
    adapter,
    schema: str,
    *,
    tables: Sequence[str] = (),
    tables_regex: Sequence[str] = (),
    tables_data: Sequence[str] = (),
    tables_data_regex: Sequence[str] = (),
    standalone: bool = False,
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

    `standalone` ajoute au mode B les tables isolees **et inutilisees**
    du schema : elles sont resolues **ici**, contre le schema reel, comme
    les listes ecrites. Le resultat est un ensemble vide sans le moindre
    echec — un schema dont toutes les tables sont reliees ou lues par du
    code ne filtre rien, et la cle posee le dira
    (`standalone_requested`).
    """
    plan = TableFilterPlan(schema=schema)
    if not (tables or tables_regex or tables_data
            or tables_data_regex or standalone):
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

    isolees: Set[str] = set()
    if standalone:
        plan.standalone_requested = True
        # Le recoupement avec `existantes` est la seule garantie que la
        # table sera reellement filtree : `DBA_TABLES` et l'export ne
        # voient pas forcement le meme objet, et une table isolee que
        # l'export ignore deviendrait une `query=` sans destinataire.
        connues = set(existantes)
        isolees = {n for n in _standalone_tables(adapter, schema)
                   if n in connues}
        mode_b |= isolees

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
    # Les isolees ne sont annoncees que si elles ont survecu au
    # chevauchement : une table a la fois isolee et listee en exclusion
    # totale est partie en entier, et la nommer ici ferait croire a deux
    # traitements pour une seule table.
    retenues = set(plan.data_only)
    plan.standalone = sorted(t for t in isolees if t in retenues)

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
