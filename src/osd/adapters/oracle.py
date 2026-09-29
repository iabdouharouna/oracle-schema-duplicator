"""Adaptateur Oracle : connexions, SQL, Data Pump.

Cet adaptateur ne connait pas SQL*Plus, `expdp` ou `impdp` : il ne fait
que construire une requete et la transmettre au `Runner`, qui l'execute
la ou se trouve la base. C'est ce qui permet d'avoir un chemin de code
identique en local et en distant, et rend le dry-run trivial a
implementer (un Runner qui enregistre au lieu d'executer).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import SQL_DELIMITER
from ..errors import ConnectionError_, OsdError, PrereqError
from ..logging_setup import get_logger
from ..redact import redact
from ..runner import Result, oracle_error_codes

LOG = get_logger()

#: Timeout plus long pour les requetes de metadonnees : `DBA_SEGMENTS` et
#: `DBA_OBJECTS` peuvent etre costly sur un schema de plusieurs milliers
#: d'objets, et `SQL*Plus` n'a pas de timeout propre.
SQL_TIMEOUT = 600

#: Connexions refusees par le listener, traduisibles en message clair.
_TRANSIENT_ORACLE = {
    "ORA-12514", "ORA-12541", "ORA-12545", "ORA-12560",
    "ORA-01034", "ORA-01017", "ORA-28000", "ORA-28009",
}


@dataclass
class OracleSide:
    """Description d'un cote de la duplication (source ou cible)."""

    name: str
    connect: str
    schema: str
    directory: str
    wallet: str = ""
    user: str = ""
    password: str = ""
    tns_admin: str = ""
    #: Connexion en `SYSDBA`. Rare en exploitation courante, mais
    #: indispensable quand le wallet ne contient que des identifiants
    #: privilegies, ou quand le schema a dupliquer n'est pas celui du
    #: compte connecte et que les droits `EXP_FULL_DATABASE` ne sont pas
    #: accordes.
    sysdba: bool = False
    runner: Any = None
    _connected: bool = field(default=False, repr=False)

    def effective_connect(self) -> str:
        """Chaine de connexion reellement transmise a SQL*Plus.

        Le preference est donnee au wallet : `/@connect` ne contient aucun
        mot de passe, ni dans la ligne de commande, ni dans le journal, ni
        dans le processus distant. Le mot de passe en clair n'est accepte
        que si aucun wallet n'est configure.
        """
        suffix = self.privilege_suffix()
        if self.wallet:
            return self.wallet_connect() + suffix
        if self.password:
            return f"{self.user or self.schema}/{self.password}@{self.connect}{suffix}"
        if self.user:
            # Forme `user@connect` sans mot de passe : acceptee par le
            # client seulement si le wallet sait resoudre l'identite, ce
            # que le precontrole de l'etape 3 doit avoir verifie.
            return f"{self.user}@{self.connect}{suffix}"
        return f"{self.connect}{suffix}"

    def wallet_connect(self) -> str:
        """Chaine de base sur le wallet externe.

        Forme `/@connect` : nom d'utilisateur ET mot de passe laisses
        vides, SQL*Plus resout alors l'identite dans le wallet externe
        declare par `SQLNET.WALLET_LOCATION` sur l'hote. C'est le seul
        mode qui garantit qu'aucun secret ne transite par le serveur de
        saut ni par la table des processus de l'hote.

        La forme `user/@connect` a ete ecartee : SQL*Plus la rejette en
        SP2-0306, la forme sans mot de passe n'etant acceptee que pour
        une identite vide.
        """
        return f"/@{self.connect}"

    def label(self) -> str:
        return f"{self.name}({self.schema})"

    def env(self) -> Dict[str, str]:
        """Variables d'environnement requises par le client Oracle.

        `TNS_ADMIN` est indispensable pour resoudre un alias TNS : sans
        lui, le client cherche `tnsnames.ora` dans
        `$ORACLE_HOME/network/admin` et dans le repertoire courant, ce
        qui echoue des que la base n'est pas locale a l'agent. La valeur
        est donc transmise par le script plutot que supposee heritee du
        profil de l'utilisateur SSH — un login non interactif n'a pas de
        `.profile` source, donc un alias TNS y serait introuvable.
        """
        env: Dict[str, str] = {}
        if self.tns_admin:
            env["TNS_ADMIN"] = self.tns_admin
        return env

    def privilege_suffix(self) -> str:
        """Suffixe de connexion : `` as sysdba`` ou chaine vide.

        Data Pump et SQL*Plus ne resaltent pas de la meme facon :
        `expdp` **refuse** `as sysdba` sur sa ligne de commande
        (LRM-00112) mais l'accepte dans un parfile, tandis que SQL*Plus
        l'accepte partout. Le choix est donc porte par la configuration,
        et chaque outil l'applique la ou il est legal.
        """
        return " as sysdba" if self.sysdba else ""


class OracleAdapter:
    """Execute des requetes et des operations Data Pump sur un cote."""

    def __init__(self, side: OracleSide) -> None:
        self.side = side
        self._verified = False

    # -- SQL -------------------------------------------------------------
    def query(self, sql: str, *, timeout: int = SQL_TIMEOUT) -> List[List[str]]:
        """Execute une requete et retourne les lignes decoupees.

        Le retour est une liste de colonnes texte. Le SQL est construit par
        l'appelant a partir d'identifiants valides (voir
        `config.validate_identifier`), jamais de saisie libre.
        """
        result = self._run_sql(sql, timeout=timeout)
        self._raise_on_oracle_error(result, "requete")
        rows: List[List[str]] = []
        for line in result.rows:
            line = line.rstrip("\r")
            if not line.strip():
                continue
            # SQL*Plus complete chaque colonne a sa largeur d'affichage :
            # meme avec `colsep`, les valeurs sont precedees d'onglets et
            # de blancs de remplissage. Le separateur reste fiable, mais
            # les champs doivent etre nettoyes — les valeurs de ce projet
            # sont des identifiants, des nombres ou des chemins, dont
            # aucun n'a de blanc significatif en extremite.
            rows.append([field.strip() for field in line.split(SQL_DELIMITER)])
        return rows

    def query_one(self, sql: str, *, timeout: int = SQL_TIMEOUT) -> Optional[List[str]]:
        """Retourne la premiere ligne, ou None si le resultat est vide."""
        rows = self.query(sql, timeout=timeout)
        return rows[0] if rows else None

    def scalar(self, sql: str, *, timeout: int = SQL_TIMEOUT) -> str:
        """Retourne la premiere colonne de la premiere ligne, ou ''."""
        row = self.query_one(sql, timeout=timeout)
        if not row:
            return ""
        return row[0].strip()

    def execute(self, sql: str, *, timeout: int = SQL_TIMEOUT) -> None:
        """Execute un enonce sans tabuler de resultat (DDL, GRANT...)."""
        result = self._run_sql(sql, timeout=timeout)
        self._raise_on_oracle_error(result, "instruction")

    def _run_sql(self, sql: str, *, timeout: int) -> Result:
        body = _body("remote_sqlplus.sh")
        argv = [self.side.effective_connect(), sql]
        script = _assemble(body, argv, env=self.side.env())
        return self.side.runner.run_script(script, timeout=timeout)

    def _raise_on_oracle_error(self, result: Result, what: str) -> None:
        codes = oracle_error_codes(result)
        rc = result.get_int("OSD_RC", result.rc)
        if not codes and rc == 0:
            return

        readable = ", ".join(codes) if codes else f"rc={rc}"
        LOG.error(
            "echec SQL %s: %s", self.side.label(), redact(readable),
        )
        message = f"erreur Oracle sur {self.side.label()} ({what}) : {readable}"
        detail = [redact(line) for line in result.stderr.splitlines() if line.strip()][:10]

        # Une erreur de connexion ou d'authentification est un code 3, pas
        # un code 7 : elle doit etre diagnostiquee par l'exploitant comme
        # un probleme de configuration reseau, avant toute reprise.
        if set(codes) & _TRANSIENT_ORACLE:
            raise ConnectionError_(
                message, detail=detail,
                hint="Verifier le listener, la chaine de connexion et le "
                     "wallet sur l'hote.",
            )
        raise PrereqError(message, detail=detail)

    # -- Verification de vie de la connexion ------------------------------
    def check_connection(self) -> Dict[str, str]:
        """Verifie la connexion et retourne les metadonnees de la base.

        Une seule requete : version, instance, mode, nom de la base. Elle
        sert a la fois de controle de connectivite (etape 4/5) et de garde
        de compatibilite pour les etapes suivantes.
        """
        # Une seule requete, une seule ligne, colonnes **nommees par un
        # litteral**. Le premier decoupage de l'analyse ne peut alors pas
        # dependre de l'ordre des colonnes, ni du fait qu'une vue en
        # expose trois et une autre une seule : `select banner_full ...
        # ; select instance_name, host_name, status ...` produit des
        # lignes de largeurs differentes, et un parseur qui lit « nom en
        # colonne 0, valeur en colonne 1 » decale silencieusement tout.
        # Chaque valeur porte ici son propre nom.
        sql = (
            "select 'version' k, "
            "  substr(banner_full, instr(banner_full, 'Release'), 11) v "
            "from v$version where rownum = 1\n"
            "union all\n"
            "select 'instance', instance_name from v$instance\n"
            "union all\n"
            "select 'host', host_name from v$instance\n"
            "union all\n"
            "select 'status', status from v$instance\n"
            "union all\n"
            "select 'dbname', name from v$database\n"
            "union all\n"
            "select 'open_mode', open_mode from v$database\n"
            "union all\n"
            "select 'cdb', cdb from v$database\n"
            "union all\n"
            "select 'session_user', sys_context('USERENV','SESSION_USER') from dual\n"
            "union all\n"
            "select 'dialect_version', banner from v$version where rownum = 1"
        )
        info: Dict[str, str] = {}
        for row in self.query(sql):
            if len(row) < 2:
                continue
            key = row[0].strip().lower()
            if key:
                info[key] = row[1].strip()

        # `banner` contient « Oracle Database 19c ... 19.0.0.0.0 ... » ;
        # `version` en est le fragment apres « Release ». Les deux sont
        # conserves : le second sert a l'affichage, le premier permet a
        # une supervision de reconnaitre l'edition.
        if "version" in info and not _VERSION_RE.match(info["version"]):
            info["version"] = _extract_version(info.get("dialect_version", ""))

        if not info:
            raise ConnectionError_(
                f"aucune metadonnee retournee par {self.side.label()}",
                hint="Le compte connecte a peut-etre des privileges reduits "
                     "sur V$INSTANCE / V$VERSION.",
            )
        self._verified = True
        return info

    # -- Introspection metier -------------------------------------------
    def directory_path(self, name: str) -> Optional[str]:
        """Retourne le chemin physique associe a un objet DIRECTORY."""
        if not name:
            return None
        sql = (
            "select directory_path from dba_directories "
            f"where directory_name = '{_lit(name)}'"
        )
        return self.scalar(sql) or None

    def schema_exists(self, schema: str) -> bool:
        """Indique si le schema existe et si son compte est ouvert.

        Le compte doit etre `OPEN` : un compte `LOCKED` ou `EXPIRED` est
        dans le dictionnaire mais inutilisable, et l'echec qui en
        resulterait a l'import serait un code 6 (import) pour une cause
        de configuration, donc diagnostiquee au mauvais endroit.
        """
        return _to_int(
            self.scalar(
                "select count(*) from dba_users where username = "
                f"'{_lit(schema)}' and account_status = 'OPEN'"
            )
        ) > 0

    def object_count(self, schema: str, *, object_type: Optional[str] = None) -> int:
        """Compte les objets d'un schema, optionnellement d'un type donne.

        Utilise pour etape 7 (schema cible non vide) et etape 15
        (validation). Le comptage passe par `DBA_OBJECTS`, qui est la
        seule vue qui donne un inventaire homogene entre les deux cotes.
        """
        if object_type:
            sql = (
                "select count(*) from dba_objects where owner = "
                f"'{_lit(schema)}' and object_type = '{_lit(object_type)}'"
            )
        else:
            sql = (
                f"select count(*) from dba_objects where owner = '{_lit(schema)}'"
            )
        try:
            return int(self.scalar(sql) or 0)
        except ValueError:  # pragma: no cover - sortie Oracle inattendue
            return 0

    def tablespaces_capacity(self, names: Sequence[str]) -> Dict[str, Dict[str, int]]:
        """Retourne taille et place libre de chaque tablespace demande.

        `DBA_FREE_SPACE` donne la place reutilisable dans les segments
        libres ; c'est la seule mesure exploitable depuis SQL, l'espace
        disque reel etant traite par `remote_space.sh`.
        """
        if not names:
            return {}
        quoted = ", ".join(f"'{_lit(n)}'" for n in names)
        sql = (
            "select tablespace_name, sum(bytes) from dba_free_space "
            f"where tablespace_name in ({quoted}) group by tablespace_name"
        )
        free = {row[0].strip().upper(): _to_int(row[1]) for row in self.query(sql) if len(row) > 1}

        sql2 = (
            "select tablespace_name, sum(bytes) from dba_data_files "
            f"where tablespace_name in ({quoted}) group by tablespace_name"
        )
        data = {row[0].strip().upper(): _to_int(row[1]) for row in self.query(sql2) if len(row) > 1}

        sql3 = (
            "select tablespace_name, sum(bytes) from dba_temp_files "
            f"where tablespace_name in ({quoted}) group by tablespace_name"
        )
        temp = {row[0].strip().upper(): _to_int(row[1]) for row in self.query(sql3) if len(row) > 1}

        out: Dict[str, Dict[str, int]] = {}
        for name in {n.upper() for n in names}:
            size = data.get(name, 0) + temp.get(name, 0)
            out[name] = {
                "bytes": size,
                "free_bytes": free.get(name, 0),
            }
        return out

    def schema_segment_bytes(self, schema: str, *, content: str = "ALL") -> int:
        """Estime la taille des donnees a dupliquer.

        Le calcul depend du contenu demande, ce qui est la consequence
        directe du choix de rendre `CONTENT` configurable :

        * `ALL` / `DATA_ONLY` : somme des segments du schema, bornee aux
          segments de donnees (les index suivent les tables) ;
        * `METADATA_ONLY` : l'export ne transporte que le DDL, la taille
          du dump est de l'ordre du nombre d'objets et non du volume.

        L'estimation est volontairement prudente : elle sert a refuser
        une duplication qui n'a pas de place, pas a dimensionner un
        tablespace.
        """
        if content == "METADATA_ONLY":
            count = self.object_count(schema)
            # Ordre de grandeur empirique : quelques dizaines de kilo-octets
            # par objet dans un dump texte compresse.
            return count * 64 * 1024
        sql = (
            "select sum(nvl(bytes, 0)) from dba_segments "
            f"where owner = '{_lit(schema)}'"
        )
        try:
            return _to_int(self.scalar(sql))
        except OsdError:
            # Un compte sans acces a DBA_SEGMENTS ne doit pas bloquer la
            # duplication : l'estimation retombe sur un plancher.
            LOG.warning(
                "estimation d'espace indisponible pour %s : plancher applique",
                schema,
            )
            return 0

    def is_asm_directory(self, path: str) -> bool:
        """Indique si un chemin DIRECTORY reside dans ASM.

        ASM ne se detecte pas de facon fiable depuis le systeme de
        fichiers. Le controle retenu est different, et plus sur : on
        interroge la destination du dump declare par le serveur, ou l'on
        verifie que le chemin existe et est accessible. Un chemin
        introuvable est traite comme non conforme, ce qui est le
        comportement sur : mieux vaut bloquer avec un message clair que
        lancer un export dont le dump sera inaccessible au transfert.
        """
        if not path:
            return False
        # Heuristique explicite pour les chemins visiblement ASM, dans le
        # cas ou le serveur autorise la lecture de la dictionnaire.
        upper = path.upper()
        return upper.startswith("+") or "/ASM/" in upper


def _body(name: str) -> str:
    from ..runner import load_body

    return load_body(name)


def _assemble(
    body: str, argv: Sequence[str], *, env: Optional[Dict[str, str]] = None
) -> str:
    from ..runner import build_script

    return build_script(body, argv, env=env)


def _lit(value: str) -> str:
    """Quote une valeur SQL en neutralisant les guillemets simples.

    Les identifiants sont deja valides par `config.validate_identifier`,
    mais la defense reste ici : un seul point d'entree pour l'echappement
    SQL du projet, plutot qu'une hypothèse de securite repartie.
    """
    return value.replace("'", "''")


def _to_int(value: Any) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


#: Version Oracle complete, telle qu'elle apparait apres « Release ».
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+\.\d+")

#: Version majeure, suffisante pour le controle de compatibilite 19c.
_MAJOR_RE = re.compile(r"^(\d+)\.")


def _extract_version(banner: str) -> str:
    """Extrait « 19.0.0.0.0 » d'une bannière `v$version`."""
    m = re.search(r"\b(\d+\.\d+\.\d+\.\d+\.\d+)\b", banner)
    return m.group(1) if m else ""


def is_19c(version: str) -> bool:
    """Indique si une version relevee appartient a la famille 19c.

    Seul le premier numero compte : 19.3, 19.20 et 19.0 sont tous 19c.
    Comparer la chaine entiere serait un piege — c'est precisement la
    raison pour laquelle le controle ne verrait pas `19.20.0.0.0`.
    """
    m = _MAJOR_RE.match(version.strip())
    return bool(m) and m.group(1) == "19"
