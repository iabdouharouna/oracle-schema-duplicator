"""Outils partages par les tests unitaires.

Ces tests ne doivent dependre ni d'une base Oracle, ni d'un reseau, ni
d'un client SQL*Plus. C'est une exigence, pas une commodite : un test
qui ne peut pas tourner sur un poste de developpement ne sera pas
execute, et une verification qui n'est pas executee ne verifie rien.

Les adaptateurs sont donc **`faux`**. Ils implementent la meme interface
que les reels, mais repondent a des valeurs declarees. Un controle
teste ainsi verifie reellement la logique du controle — le calcul de
marge, la hierarchie des codes, le refus d'un schema existant — et non
la connectivite d'un serveur.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# `src` est ajoute au `sys.path` pour que les tests soient lancables
# depuis la racine du depot **sans installation** :
#
#     python3 -m unittest discover -s tests/unit -t .
#
# Un projet qui exige `pip install -e .` pour etre teste n'est pas
# testable sur l'hote d'exploitation, ou l'on n'installe generalement
# rien.
_ROOT = Path(__file__).resolve().parent.parent.parent
#: Racine des sources, reutilisable par les tests qui lancent un
#: **second processus** (verrou, concurrence). Ces tests doivent
#: transmettre le chemin au lieu de supposer que le module est
#: installe : un outil de production n'est pas installe, et un test qui
#: l'exigerait ne tournerait que sur le poste du developpeur.
SRC_DIR = _ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


# --------------------------------------------------------------------------
# Adaptateur Oracle simule
# --------------------------------------------------------------------------

class FakeAdapter:
    """Adaptateur Oracle a reponses declarees.

    Chaque methode de l'interface reelle lit un attribut homonyme. Deux
    mecanismes distincts, parce que les requetes sont de deux natures :

    * les **methodes** (`schema_exists`, `object_count`,
      `tablespaces_capacity`…) sont posees par nom ;
    * les **requetes SQL** passent par `responses`, un dictionnaire
      associant un **fragment** de SQL a des lignes. Le controle de
      privileges et la liste des tablespaces passent tous deux par
      `adapter.query(...)` avec des SQL differents, et les distinguer
      par une valeur unique les confondrait — le test passerait alors
      que la requete est erronee.

    Une valeur de type `Exception` est levee telle quelle : c'est ce qui
    permet de tester les chemins d'erreur (connexion refusee, privilege
    illisible) sans avoir a faire echouer un client simule.

    Les valeurs non declarees renvoient un `zero` du bon type plutot
    qu'une `AttributeError` : un test qui oublie de declarer une donnee
    doit echouer sur l'assertion, pas sur une erreur de code de test.
    """

    def __init__(self, **data: Any) -> None:
        self._data: Dict[str, Any] = dict(data)
        #: Fragment de SQL -> lignes. Recherche par ordre d'insertion,
        #: premier fragment trouve gagnant.
        self.responses: Dict[str, Any] = dict(
            self._data.pop("responses", {}) or {}
        )
        self.queries: List[str] = []
        #: DDL reellement executees. Une liste vide est le temoin d'un
        #: `check` ou d'un dry-run : le compte a ete decrit, pas cree.
        self.ddl: List[str] = []
        self.connect_info: Dict[str, str] = self._data.pop(
            "_connect_info",
            {"version": "19.0.0.0.0", "instance": "FAKE", "dbname": "FAKE",
             "status": "OPEN", "open_mode": "READ WRITE", "cdb": "NO",
             "session_user": "SYS", "host": "localhost"},
        )
        self.verified = False
        #: Runner par lequel un DDL part quand le faux en porte un.
        #: Poses par le harnais de scenario, jamais par le test : c'est
        #: lui qui sait si la simulation autorise l'ecriture.
        self.runner: Any = None

    # -- Generique -------------------------------------------------------

    def _get(self, name: str, default: Any) -> Any:
        if name not in self._data:
            return default
        value = self._data[name]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, type) and issubclass(value, BaseException):
            raise value()
        return value

    # -- Interface OracleAdapter -----------------------------------------

    def check_connection(self) -> Dict[str, str]:
        self.verified = True
        value = self._get("check_connection", self.connect_info)
        if isinstance(value, dict):
            return value
        return self.connect_info

    def query(self, sql: str) -> List[List[str]]:
        self.queries.append(sql)
        normalise = " ".join(sql.lower().split())
        for fragment, lignes in self.responses.items():
            cle = " ".join(fragment.lower().split())
            if cle in normalise:
                if isinstance(lignes, BaseException):
                    raise lignes
                if isinstance(lignes, type) and issubclass(lignes, BaseException):
                    raise lignes()
                return list(lignes)
        return []

    def scalar(self, sql: str) -> str:
        lignes = self.query(sql)
        if not lignes or not lignes[0]:
            return ""
        return str(lignes[0][0])

    def execute(self, sql: str) -> None:
        """Simule un DDL.

        Quand le faux porte un `runner`, c'est **lui** qui decide : en
        simulation, le `NullRunner` retient la mutation et n'ecrit rien.
        Sans ce passage, un `dry-run` ferait apparaitre le compte cree
        dans le faux, et le test qui cherche a prouver qu'aucune ecriture
        n'a lieu le verifierait sur un temoin deja ecrit — il passerait
        en regardant le mauvais endroit.

        Le script est celui du vrai, `build_script` compris : c'est lui
        qui pose les `osd_argN=`, et c'est de la que `_arguments` tire
        le resume du rapport. Passer le SQL nu produirait un resume
        vide, et le test verifierait alors un rapport que la chaine
        reelle n'emplit pas.
        """
        from osd.runner import build_script

        self.queries.append(sql)
        runner = getattr(self, "runner", None)
        if runner is not None:
            runner.run_script(build_script("true", [sql]), mutating=True)
            if not runner.allows_mutation():
                return
        self.ddl.append(sql)

    def object_count(self, schema: str, *, object_type: str = "") -> int:
        """Compte d'objets, filtrable par type.

        Le total et le nombre d'invalides sont deux reponses
        **differentes** : les confondre en une valeur unique ferait
        passer les objets invalides pour des objets quelconques, et le
        test de l'avertissement « objets invalides » echouerait. La
        valeur fournie peut donc etre :

        * un entier, utilise pour toute requete ;
        * un dictionnaire ``{"": 30, "INVALID": 2}``, cle `""` valant
          le total ; c'est la forme a employer pour le cas interessant.
        """
        self.queries.append(f"object_count {schema} {object_type}")
        valeur = self._get("object_count", 0)
        if isinstance(valeur, dict):
            return int(valeur.get(object_type, valeur.get("", 0)))
        return int(valeur)

    def schema_exists(self, schema: str) -> bool:
        self.queries.append(f"schema_exists {schema}")
        return bool(self._get("schema_exists", True))

    def schema_segment_bytes(
        self,
        schema: str,
        *,
        content: str = "ALL",
        segment_signatures: Sequence[str] = (),
        object_signatures: Sequence[str] = (),
    ) -> int:
        self.queries.append(f"schema_segment_bytes {schema} {content}")
        # Les deux listes viennent du plan de filtrage de l'etape 6. On
        # les compte ici : un test qui verifie que le filtre arrive bien
        # jusqu'a l'estimation le peut sans reconstruire le plan.
        if segment_signatures:
            self.queries.append(f"segment_signatures {len(segment_signatures)}")
        if object_signatures:
            self.queries.append(f"object_signatures {len(object_signatures)}")
        return int(self._get("schema_segment_bytes", 0))

    def directory_path(self, name: str) -> str:
        self.queries.append(f"directory_path {name}")
        return str(self._get("directory_path", "/tmp"))

    def is_asm_directory(self, path: str) -> bool:
        return str(self._get("is_asm_directory", False)).lower() in ("1", "true", "oui")

    def tablespaces_capacity(self, names: Sequence[str]) -> Dict[str, Dict[str, int]]:
        self.queries.append("tablespaces_capacity")
        return dict(self._get("tablespaces_capacity", {}))

    def label(self) -> str:
        return "fake"


# --------------------------------------------------------------------------
# Adaptateur « filtrage de tables »
# --------------------------------------------------------------------------

class SourceFiltre(FakeAdapter):
    """Schema source dont les requetes du filtre sont resolues comme
    Oracle les resoudrait.

    Le filtrage lit desormais les vues de dictionnaire **en entier** pour
    le schema, et recoupe en Python : aucune de ses requetes ne porte de
    liste `IN`. Le faux repond donc sans lire de liste, et rend tout ce
    que la vue contiendrait pour le schema.

    Le reste (inventaire, privileges, etat des jobs) retombe sur le
    mecanisme habituel : un faux qui repondrait a tout depuis son propre
    catalogue repondrait mal aux etapes qu'il ne teste pas — notamment
    l'etape 16, qui compare deux inventaires.

    Donnees attendues, toutes facultatives :

    * `tables` — les tables du schema ;
    * `possedes` — table -> objets qui partent avec elle (la table
      comprise) : la cascade reelle de `EXCLUDE=TABLE` ;
    * `dependants` — objet -> objets qui le referencent. Sert la
      fermeture du mode A, et aussi le volet « utilisee » de la regle des
      tables isolees : un dependant dont le type (lu dans `signatures`)
      n'est pas `TABLE` disqualifie la table qu'il reference — sauf un
      trigger **pose sur cette table**, dont la base est relue dans
      `possedes` ;
    * `signatures` — paires `[type, nom]`, ordonnees comme la requete
      les rend, et source unique des signatures `TYPE~NOM` ;
    * `fk_sortantes` / `fk_entrantes` — tables du schema reliees par une
      cle etrangere, respectivement en sortie et en entree. Les deux sont
      declarees separement parce que ce sont les deux moities de la
      definition d'une table isolee : les confondre ne dirait pas
      laquelle des deux conditions a repondu. La requete `DBA_TABLES` les
      reunit, comme la definition le fait.
    """

    def query(self, sql: str) -> List[List[str]]:
        reponse = self._reponse_filtre(sql)
        if reponse is not None:
            self.queries.append(sql)
            return reponse
        return super().query(sql)

    def _reponse_filtre(self, sql: str) -> Optional[List[List[str]]]:
        bas = " ".join(sql.lower().split())

        if "from dba_dependencies" in bas:
            table: Dict[str, List[str]] = self._get("dependants", {})
            if "referenced_type = 'table'" in bas:
                # Volet « utilisee » de `_used_tables` : triples
                # `(table referencee, nom du referenceur, type)`. Le type
                # est relu dans l'inventaire declare — un nom seul ne dit
                # pas si le referenceur est une vue, un package ou une
                # table. Un referenceur absent de l'inventaire est rendu
                # comme `TABLE` : non etabli comme un usage, il ne
                # disqualifie pas.
                type_par_nom = {str(sig[1]): str(sig[0])
                                for sig in self._get("signatures", [])
                                if len(sig) > 1}
                return [[reference, dependant,
                         type_par_nom.get(dependant, "TABLE")]
                        for reference, dependants in table.items()
                        for dependant in dependants]
            # Toutes les aretes du schema : la fermeture se fait cote
            # code, pas cote requete.
            return [[reference, dependant]
                    for reference, dependants in table.items()
                    for dependant in dependants]

        if "select trigger_name, table_name from dba_triggers" in bas:
            # Base de chaque trigger : un trigger voyage avec la table sur
            # laquelle il est pose, et `possedes` la nomme. C'est cette
            # correspondance qui distingue un trigger propre a la table
            # (il ne la disqualifie pas) d'un trigger d'une autre table.
            type_par_nom = {str(sig[1]): str(sig[0])
                            for sig in self._get("signatures", [])
                            if len(sig) > 1}
            possedes: Dict[str, List[str]] = self._get("possedes", {})
            return [[objet, table]
                    for table, objets in possedes.items()
                    for objet in objets
                    if type_par_nom.get(objet) == "TRIGGER"]

        if "from dba_tables" in bas:
            # La definition d'une table isolee : hors de la reunion des
            # FK sortantes et entrantes. Oracle repond `DBA_TABLES` moins
            # les deux `NOT EXISTS` ; le faux recompose le meme ensemble.
            liees = set(self._get("fk_sortantes", []))
            liees |= set(self._get("fk_entrantes", []))
            return [[t] for t in self._get("tables", [])
                    if t not in liees]

        if ("from dba_indexes" in bas or "from dba_triggers" in bas
                or "from dba_lobs" in bas):
            # La cascade, table a table. Oracle rend des paires
            # `(table_name, nom d'objet)` ; le code ne garde que les
            # tables qu'il a filtrees.
            possedes: Dict[str, List[str]] = self._get("possedes", {})
            return [[table, objet]
                    for table, objets in possedes.items()
                    for objet in objets]

        if "select object_type, object_name from dba_objects" in bas:
            return [list(sig) for sig in self._get("signatures", [])]

        if "object_name not like 'bin$%'" in bas:
            return [[nom] for nom in self._get("tables", [])]

        return None


# --------------------------------------------------------------------------
# Adaptateur « dependances »
# --------------------------------------------------------------------------

class FakeRunner:
    """Runner qui repond a `has_binary` et refuse d'executer quoi que ce soit."""

    kind = "local"

    def __init__(self, *, present: Sequence[str] = (), host: str = "fakehost") -> None:
        self._present = set(present)
        self.host = host
        self.user = ""
        self.probe_dir = "/tmp"
        self.scripts: List[str] = []

    @property
    def label(self) -> str:
        return f"fake:{self.host}"

    def has_binary(self, name: str) -> bool:
        return name in self._present

    def allows_mutation(self) -> bool:
        return True

    def run_script(self, script: str, *, timeout=None, mutating: bool = False):
        raise AssertionError(
            "FakeRunner ne doit jamais executer de script : le code teste "
            "aurait du prendre une decision avant d'appeler le runner."
        )


# --------------------------------------------------------------------------
# Isolation du profil de connexion
# --------------------------------------------------------------------------

#: Repertoire vide qui remplace `HOME` pendant un test.
#:
#: Il ne doit contenir **aucun** fichier `.profile`, `.profile.ksh` ni
#: `.login` : c'est l'absence de ces fichiers qui rend le sourcing inerte.
_HOMES_VIDES: List[str] = []


def isoler_home(cas: Any) -> str:
    """Detourne `HOME` vers un repertoire vide, jusqu'à la fin du test.

    Les scripts generes sourcent le profil de connexion -- `/etc/profile`,
    puis `~/.profile` -- parce qu'un client Oracle sur AIX est dans le
    `PATH` du compte d'exploitation, pose par ces fichiers, et qu'une
    coquille non interactive ne les source pas d'elle-meme. C'est le
    comportement voulu en exploitation, et un test qui l'ignore mesure
    autre chose.

    Le piege est d'une nature deja connue de cette suite : un test qui
    depend de son environnement. Ici, le developpeur dont `~/.profile`
    declare `ORACLE_HOME` voit ses tests « client absent » passer au vert
    en executant le **vrai** client, et ses faux clients Data Pump se
    retrouver derriere lui -- un profil qui *prepend* son chemin a
    `PATH` suffit a les repousser. Le developpeur voit un vert,
    l'integration voit un 127 : exactement l'echec que
    `path_sans_client_reel` (test_datapump.py) existe pour empecher.

    `HOME` ne rend donc pas sa valeur a un `mock.patch.dict` : le test
    doit pouvoir heriter de la restauration, et un gestionnaire de
    contexte imbrique ici masquerait la vraie cause d'un `Path` absent
    plus loin.

    Reserve a `/etc/profile` : le neutraliser demanderait d'ecrire dans
    un fichier systeme, ce qu'un test n'a pas le droit de faire. Un hote
    qui y declarerait `ORACLE_HOME` resterait donc contaminant, mais c'est
    aussi un hote ou `path_sans_client_reel` doit etre elargi. Le siege
    n'est pas change.
    """
    if not _HOMES_VIDES:
        racine = tempfile.mkdtemp(prefix="osd-home-")
        os.chmod(racine, 0o700)
        _HOMES_VIDES.append(racine)
    home = _HOMES_VIDES[0]
    precedent = os.environ.get("HOME")
    cas.addCleanup(_restaurer_home, precedent)
    os.environ["HOME"] = home
    return home


def _restaurer_home(precedent: Optional[str]) -> None:
    if precedent is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = precedent


# --------------------------------------------------------------------------
# Configuration de test
# --------------------------------------------------------------------------

#: Surcharges minimales qui rendent une configuration valide. Toute
#: configuration de test part de la, et ne modifie que ce qu'il teste :
#: c'est ce qui evite qu'un test echoue pour une raison sans rapport
#: avec son objet, parce qu'un defaut du schema aurait change.
BASE_OVERRIDES: Dict[str, str] = {
    "SOURCE_SCHEMA": "SRC",
    "TARGET_SCHEMA": "TGT",
    "SOURCE_CONNECT": "L_SRC",
    "TARGET_CONNECT": "L_TGT",
    "SOURCE_DIRECTORY": "DP_DIR",
    "TARGET_DIRECTORY": "DP_DIR",
    "SOURCE_TNS_ADMIN": "/opt/oracle/network/admin",
    "TARGET_TNS_ADMIN": "/opt/oracle/network/admin",
    "LOG_DIR": "logs",
    "WORK_DIR": "work",
    "REPORT_DIR": "reports",
    "SOURCE_SYSDBA": "true",
    "TARGET_SYSDBA": "true",
}


def overrides(**extra: Any) -> Dict[str, str]:
    """Surcharges completes a partir de `BASE_OVERRIDES`."""
    out = dict(BASE_OVERRIDES)
    for key, value in extra.items():
        out[key.upper()] = value
    return out


#: Inventaire et mot de passe de coffre employes par les tests.
#:
#: Ils sont crees a la demande et partages : la validation de la
#: configuration **exige** leur existence des qu'un hote est designe,
#: donc toute configuration de test qui pose `SOURCE_HOST` doit pouvoir
#: les fournir. Les creer ici evite que chaque test distant doive
#: reproduce la mise en place -- et surtout evite 185 echecs issus d'une
#: seule cause, ce qui masque la cause reelle quand elle existe.
#:
#: Le contenu est sans importance : aucun test n'authentifie reellement.
#: Ce qui compte est que les fichiers existent, sont lisibles, et que le
#: mot de passe de coffre est en 0600 -- le mode que la validation
#: controle.
_TRANSPORT: Dict[str, str] = {}


def _transport() -> Dict[str, str]:
    """Prepare l'inventaire Ansible et le coffre, une fois pour toutes."""
    if _TRANSPORT:
        return _TRANSPORT
    racine = Path(tempfile.mkdtemp(prefix="osd-transport-"))
    inventaire = racine / "hosts"
    inventaire.write_text(
        "source.exemple ansible_connection=local\n"
        "cible.exemple ansible_connection=local\n",
        encoding="utf-8",
    )
    coffre = racine / "vault-pass"
    coffre.write_text("mot-de-passe-de-test\n", encoding="utf-8")
    os.chmod(coffre, 0o600)
    _TRANSPORT.update(
        OSD_INVENTORY=str(inventaire),
        OSD_VAULT_PASSWORD_FILE=str(coffre),
    )
    return _TRANSPORT


def load_config(**extra: Any):
    """Charge une configuration valide completee par `extra`.

    Les surcharges de l'appelant priment sur celles de `extra`, sans quoi
    un test ne pourrait pas remplacer l'inventaire par un chemin qu'il
    controle -- ce dont plusieurs tests de validation ont besoin pour
    verifier le refus d'un inventaire absent.
    """
    from osd import config as config_mod

    base = dict(BASE_OVERRIDES)
    if any(key.endswith("_HOST") and value for key, value in extra.items()):
        # L'inventaire n'a de sens qu'avec un hote : le poser en
        # permanence masquerait le test « hote designe mais inventaire
        # absent », qui verifie précisément ce refus.
        base.update(_transport())
    base.update(overrides(**extra))
    return config_mod.load(None, overrides=base)
