r"""Chargement et validation de la configuration.

Choix structurant : le fichier `.conf` n'est **jamais** execute par le
shell. Il est lu ligne a ligne par un parseur strict, avec une liste
 blanche de cles et un typage explicite. Un fichier de configuration est
une entree non fiable : le `source`/`eval` d'un fichier tiers reviendrait
a executer du code arbitraire avec les privileges de l'operateur, ce qui
est incompatible avec l'exigence « ne jamais utiliser eval ».

Syntaxe acceptee, volontairement minimale :

    # commentaire
    CLE=VALEUR
    CLE="VALEUR avec espaces"
    CLE='VALEUR'

Tout le reste est rejete : `export`, substitution `$(...)`, backquotes,
point-virgule, chainage `&&`, heredoc, continuation par `\`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import ConfigError
from .redact import redact

#: Identifiant Oracle. Volontairement strict : il sert aussi de garde-fou
#: contre l'injection dans le SQL que l'on construit par concatenation.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,29}$")

#: Nom d'objet DIRECTORY Oracle (meme alphabet, 30 caracteres max).
_OBJECT_NAME_RE = _IDENT_RE

#: Separateur de sortie SQL*Plus utilise par l'adaptateur Oracle.
SQL_DELIMITER = "~|"

_LINE_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

#: Constructions shell refusees meme si le parseur n'executerait rien.
#: Le but est double : eviter une reutilisation ulterieure par un `source`,
#: et interdire les valeurs qui ne pourraient pas etre transportees
#: proprement vers les scripts distants.
_FORBIDDEN_TOKENS = ("$(", "`", ";", "&&", "||", ">", "<", "\n", "\\\\")

CONTENT_CHOICES = ("ALL", "DATA_ONLY", "METADATA_ONLY")
COMPRESSION_CHOICES = ("ALL", "DATA_ONLY", "METADATA_ONLY", "NONE")
TABLE_EXISTS_ACTIONS = ("SKIP", "APPEND", "REPLACE", "TRUNCATE")
VALIDATION_LEVELS = ("MINIMAL", "STANDARD", "FULL")
EXEC_MODES = ("local", "remote")
TRANSFER_BACKENDS = ("auto", "rsync", "scp", "scp-legacy", "sftp")

#: Valeurs de TABLE_EXISTS_ACTION consideres destructives : elles ecrivent
#: ou detruisent des donnees deja presentes dans le schema cible. Elles
#: exigent `--allow-destructive`, sinon l'outil echoue en code 8.
DESTRUCTIVE_ACTIONS = frozenset({"REPLACE", "TRUNCATE"})

#: Cles d'authentification SSH retirees. Elles restent decrites dans le
#: schema pour qu'un fichier de configuration existant soit lu sans
#: erreur de cle inconnue, mais toute valeur portee declenche un refus
#: explicite : l'exploitant doit savoir que le secret n'est plus lu.
_OBSOLETE_SSH_KEYS = (
    "SSH_KEY",
    "OS_SSH_USER",
    "SOURCE_SSH_USER",
    "TARGET_SSH_USER",
    "SOURCE_SSH_OPTS",
    "TARGET_SSH_OPTS",
)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """Description d'une cle de configuration."""

    kind: str  # str | bool | int | enum | csv | password
    default: Any
    choices: Tuple[str, ...] = ()
    secret: bool = False
    doc: str = ""


def _schema() -> Dict[str, Spec]:
    return {
        # -- Execution et journalisation --------------------------------
        "RUN_ID": Spec("str", "", doc="Identifiant de run; genere si vide."),
        "LOG_LEVEL": Spec("enum", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
        "LOG_DIR": Spec("str", "logs"),
        "WORK_DIR": Spec("str", "work"),
        "REPORT_DIR": Spec("str", "reports"),
        "LOCK_DIR": Spec("str", "", doc="Vide => WORK_DIR. Doit etre local, pas NFS."),
        "KEEP_ARTIFACTS": Spec("bool", False, doc="Ne nettoie ni dump ni parfile."),
        # -- Source ------------------------------------------------------
        "SOURCE_CONNECT": Spec("str", "", doc="EZCONNECT, alias TNS ou SCAN."),
        "SOURCE_SCHEMA": Spec("str", ""),
        "SOURCE_DIRECTORY": Spec("str", "", doc="Objet DIRECTORY Oracle de l'export."),
        "SOURCE_WALLET": Spec("str", "", doc="Chemin du wallet; jamais de mot de passe."),
        "SOURCE_USER": Spec("str", "", doc="Utilisateur applicatif."),
        "SOURCE_SYSDBA": Spec("bool", False, doc="Connexion source en SYSDBA."),
        "SOURCE_OS_AUTH": Spec(
            "bool", False,
            doc="Authentification OS : connexion locale par l'identite du "
                "compte d'exploitation (`/`). Incompatible avec "
                "SOURCE_CONNECT et SOURCE_WALLET.",
        ),
        "SOURCE_TNS_ADMIN": Spec("str", "", doc="TNS_ADMIN distant si alias TNS."),
        # -- Cible --------------------------------------------------------
        "TARGET_CONNECT": Spec("str", ""),
        "TARGET_SCHEMA": Spec("str", ""),
        "TARGET_DIRECTORY": Spec("str", "", doc="Objet DIRECTORY Oracle de l'import."),
        "TARGET_WALLET": Spec("str", ""),
        "TARGET_USER": Spec("str", ""),
        "TARGET_TNS_ADMIN": Spec("str", ""),
        "TARGET_SYSDBA": Spec("bool", False, doc="Connexion cible en SYSDBA."),
        "TARGET_OS_AUTH": Spec(
            "bool", False,
            doc="Authentification OS : connexion locale par l'identite du "
                "compte d'exploitation (`/`). Incompatible avec "
                "TARGET_CONNECT et TARGET_WALLET.",
        ),
        # -- Execution distante ------------------------------------------
        # `*_HOST` vide => execution locale (le serveur de saut heberge la
        # base). Renseigne => execution par **Ansible** sur l'hote Oracle,
        # seul cas ou `*_TNS_ADMIN` et `*_DIRECTORY` ont un sens.
        #
        # `*_HOST` designe un **nom d'inventaire**, pas une adresse : si
        # l'inventaire pose `ansible_host`, les deux peuvent differer.
        #
        # Les identifiants SSH ne sont plus des cles de configuration.
        # Ils vivent dans l'inventaire, chiffres par Ansible Vault, et
        # Ansible est le seul processus a les voir. Les englober ici les
        # mettrait dans un fichier ordinaire, donc dans une sauvegarde,
        # et dans la sauvegarde de cette sauvegarde.
        #
        # `StrictHostKeyChecking=accept-new` reste le defaut cote
        # inventaire : il accepte une premiere connexion (indispensable en
        # environnement neuf) mais refuse toute cle qui change, ce qui
        # protege du vol de session. `yes` bloquerait un run cron sur un
        # hote jamais contacte ; `no` accepterait une cle forgee.
        "SOURCE_HOST": Spec("str", "", doc="Nom d'inventaire de l'hote source. Vide = local."),
        "TARGET_HOST": Spec("str", "", doc="Nom d'inventaire de l'hote cible. Vide = local."),
        "OSD_INVENTORY": Spec("str", "inventory/hosts",
                              doc="Inventaire Ansible designant les hotes."),
        "OSD_VAULT_PASSWORD_FILE": Spec("str", "", secret=True,
                                         doc="Fichier de mot de passe du coffre. "
                                             "Hors du depot, en 0600."),

        # -- Data Pump ----------------------------------------------------
        "CONTENT": Spec("enum", "ALL", CONTENT_CHOICES),
        "COMPRESSION": Spec("enum", "ALL", COMPRESSION_CHOICES),
        "PARALLEL": Spec("int", 4),
        "JOB_PREFIX": Spec("str", "OSD"),
        "FILESIZE_MB": Spec("int", 0, doc="0 = illimite."),
        "REMAP_TABLESPACE": Spec("csv", "", doc="Ex SRC_TS:DST_TS,SRC2:DST2."),
        "EXCLUDE": Spec("csv", "", doc="Ex TABLE:DEPT,VIEW:HR.V_TEST"),
        "INCLUDE": Spec("csv", "", doc="Ex TABLE:DEPT"),
        # -- Securite et gardes-fous -------------------------------------
        "ALLOW_EXISTING_TARGET": Spec(
            "bool", False, doc="Refuse par defaut d'ecraser un schema cible existant."
        ),
        "CREATE_TARGET_SCHEMA": Spec(
            "bool", False,
            doc="Cree le schema cible absent, a l'image du schema source "
                "(tablespace, profil, privileges, quota, empreinte du mot "
                "de passe).",
        ),
        "TABLE_EXISTS_ACTION": Spec("enum", "SKIP", TABLE_EXISTS_ACTIONS),
        "ALLOW_DESTRUCTIVE": Spec("bool", False, doc="REPLACE/TRUNCATE exigent --allow-destructive."),
        "PASSWORD": Spec("password", "", secret=True, doc="Repli uniquement. Preferer le wallet."),
        # -- Transfert ---------------------------------------------------
        # `AUTO` sonde les mecanismes et retient le premier qui fonctionne
        # reellement (cf. adapters/transfer.py). Les trois autres forcent
        # un mecanisme : utile quand le diagnostic d'auto a deja ete fait,
        # et utile surtout parce qu'un echec en mode force doit remonter
        # tel quel au lieu d'etre masque par un repli silencieux.
        "TRANSFER_MODE": Spec("enum", "AUTO", ("AUTO", "LOCAL", "SCP", "SCP-LEGACY", "RSYNC", "SFTP")),
        # `SSH_KEY` et les quatre `*_SSH_USER` sont recus pour ne pas
        # casser brutalement une configuration existante, mais refuses
        # des qu'ils portent une valeur. Les accepter en silence serait
        # pire que les refuser : l'exploitant croirait s'authentifier
        # par cle alors que le secret ne serait lu par personne.
        "SSH_KEY": Spec("str", "", doc="Obsolete. L'authentification SSH est "
                             "dans l'inventaire Ansible, chiffree par Vault."),
        "OS_SSH_USER": Spec("str", "", doc="Obsolete. Compte SSH dans "
                             "l'inventaire (ansible_user)."),
        "SOURCE_SSH_USER": Spec("str", "", doc="Obsolete. Voir OS_SSH_USER."),
        "TARGET_SSH_USER": Spec("str", "", doc="Obsolete. Voir OS_SSH_USER."),
        "SOURCE_SSH_OPTS": Spec("csv", "", doc="Obsolete. Options SSH dans "
                               "l'inventaire (ansible_ssh_common_args)."),
        "TARGET_SSH_OPTS": Spec("csv", "", doc="Obsolete. Voir SOURCE_SSH_OPTS."),
        # `STAGING_DIR` et `REMOTE_TRANSFER` sont des cles de compatibilite
        # avec une premiere version de la configuration. Elles sont
        # acceptees pour ne pas casser un fichier existant, mais refusees
        # explicitement si elles portent une valeur, car aucun code ne les
        # honore : une option acceptee et ignoree est pire qu'une option
        # refusee, elle laisse croire a un comportement qui n'existe pas.
        "STAGING_DIR": Spec("str", "", doc="Obsolete. Le transfert direct "
                             "par le serveur de saut est le seul mode."),
        "REMOTE_TRANSFER": Spec("bool", False, doc="Obsolete : le transfert "
                                "passe toujours par le serveur de saut."),
        # -- Validation --------------------------------------------------
        "VALIDATION_LEVEL": Spec("enum", "STANDARD", VALIDATION_LEVELS),
        "SPACE_MARGIN_PERCENT": Spec("int", 10),
        "SPACE_MARGIN_ABS_MB": Spec("int", 512),
        "MIN_FREE_SPACE_MB": Spec("int", 0),
        # -- Rapport et mode ---------------------------------------------
        "DRY_RUN": Spec("bool", False, doc="Force le mode simulation."),
        "CLEANUP_AFTER_SUCCESS": Spec("bool", True),
        "CLEANUP_ON_FAILURE": Spec("bool", False, doc="Destructif, requiert --allow-destructive."),
        "REPORT_FORMAT": Spec("enum", "TEXT", ("TEXT", "JSON", "BOTH")),
    }


SCHEMA = _schema()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _strip_quotes(value: str, key: str, lineno: int, *, allow_empty: bool) -> str:
    """Retire les guillemets eventuels d'une valeur.

    Une valeur vide est acceptee si et seulement si la cle n'a pas de
    defaut non vide : `SOURCE_USER=` est un desangement legitime (le
    compte par defaut du systeme convient), alors que `PARALLEL=` est une
    ligne oubliee que le defaut masquerait. Le traitement est laisse a
    `load`, qui emet un avertissement dans ce dernier cas.
    """
    if not value:
        if allow_empty:
            return ""
        raise ConfigError(
            f"ligne {lineno}: {key} sans valeur",
            hint="Ecrire CLE=valeur. Pour vider une liste, retirer la ligne "
                 "et la documenter en commentaire.",
        )
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        # Un guillemet simple est litteral en POSIX : on refuse seulement
        # l'asymetrie, pas les quotes internes.
        if value[0] == '"' and '"' in inner:
            raise ConfigError(
                f"ligne {lineno}: guillemets non fermes dans {key}"
            )
        return inner
    if value[0] in "\"'":
        raise ConfigError(f"ligne {lineno}: guillemet non ferme pour {key}")
    return value


def parse_conf(text: str, origin: str = "<memoire>") -> Dict[str, str]:
    """Parse un contenu `.conf` et retourne un dictionnaire clef/valeur.

    Leve `ConfigError` (code 1) a la premiere anomalie : un fichier de
    configuration doit etre valide en totalite ou refuse, jamais interprete
    partiellement.
    """
    result: Dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            raise ConfigError(
                f"{origin} ligne {lineno}: `export` interdit, ecrivez CLE=VALEUR"
            )
        m = _LINE_RE.match(line)
        if not m:
            raise ConfigError(
                f"{origin} ligne {lineno}: syntaxe invalide "
                f"(attendu CLE=VALEUR) : {redact(line)!r}"
            )
        key = m.group("key")
        if key not in SCHEMA:
            raise ConfigError(
                f"{origin} ligne {lineno}: cle inconnue {key}",
                hint="Voir config/config.example.conf pour les cles valides.",
            )
        # Une valeur vide n'est admise que pour une cle dont le defaut est
        # lui-meme vide ; sinon elle signale une ligne oubliee, et le
        # defaut s'appliquerait silencieusement.
        value = _strip_quotes(
            m.group("value").strip(), key, lineno,
            allow_empty=SCHEMA[key].default in ("", None),
        )
        for token in _FORBIDDEN_TOKENS:
            if token in value:
                raise ConfigError(
                    f"{origin} ligne {lineno}: construction shell interdite "
                    f"({token!r}) dans la valeur de {key}"
                )
        if key in result:
            raise ConfigError(f"{origin} ligne {lineno}: cle dupliquee {key}")
        result[key] = value
    return result


def _default(spec: Spec) -> Any:
    """Retourne le defaut d'une cle, deja converti a son type final.

    Le defaut est ecrit dans le schema sous forme de chaine, pour rester
    lisible. Il doit cependant etre converti comme une valeur saisie,
    sinon une cle `csv` aurait pour defaut une chaine au lieu d'une
    liste, et les verifications qui parcourent cette liste echoueraient.
    """
    if spec.default in ("", None):
        return [] if spec.kind == "csv" else spec.default
    return _coerce("(defaut)", str(spec.default), spec)


def _coerce(key: str, raw: str, spec: Spec) -> Any:
    where = f"{key}"
    if spec.kind == "str" or spec.kind == "password":
        return raw
    if spec.kind == "bool":
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
        raise ConfigError(f"{where}: valeur booleen invalide {raw!r}")
    if spec.kind == "int":
        try:
            return int(raw.strip())
        except ValueError:
            raise ConfigError(f"{where}: entier attendu, obtenu {raw!r}") from None
    if spec.kind == "enum":
        up = raw.strip().upper()
        if up not in spec.choices:
            raise ConfigError(
                f"{where}: valeur invalide {raw!r}",
                hint=f"Valeurs acceptees : {', '.join(spec.choices)}",
            )
        return up
    if spec.kind == "csv":
        return [p.strip() for p in raw.split(",") if p.strip()]
    raise AssertionError(f"kind inconnu: {spec.kind}")  # pragma: no cover


def validate_identifier(value: str, what: str) -> str:
    """Valide un identifiant Oracle et le renvoie en majuscules.

    Les identifiants Oracle sont case-insensibles en DDL mais stockes en
    majuscules par le dictionnaire. Les normaliser ici evite des ecarts
    invisibles lors de la comparaison source/cible.
    """
    if not value:
        raise ConfigError(f"{what} absent de la configuration")
    if not _IDENT_RE.match(value):
        raise ConfigError(
            f"{what} invalide : {value!r}",
            hint="1 a 30 caracteres, lettre initiale, puis lettres, chiffres, _ $ #",
        )
    return value.upper()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass
class Config:
    """Configuration validee et normalisee."""

    values: Dict[str, Any] = field(default_factory=dict)
    source: Optional[Path] = None
    overrides: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def __getattr__(self, item: str) -> Any:  # pragma: no cover - acces simple
        # `values` peut ne pas exister encore pendant l'initialisation du
        # dataclass : on evite alors toute recursion.
        values = self.__dict__.get("values")
        if values is None:
            raise AttributeError(item)
        try:
            return values[item]
        except KeyError:
            raise AttributeError(item) from None

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    # -- Acces derives ---------------------------------------------------
    @property
    def source_schema(self) -> str:
        return self.values["SOURCE_SCHEMA"]

    @property
    def target_schema(self) -> str:
        return self.values["TARGET_SCHEMA"]

    @property
    def has_password(self) -> bool:
        return bool(self.values.get("PASSWORD"))

    def secret_keys(self) -> List[str]:
        return [k for k, s in SCHEMA.items() if s.secret and self.values.get(k)]

    def as_safe_dict(self) -> Dict[str, Any]:
        """Vue serialisable, valeurs sensibles remplacees par `MASK`."""
        out: Dict[str, Any] = {}
        for key, value in self.values.items():
            spec = SCHEMA.get(key)
            out[key] = "***" if (spec and spec.secret) else value
        return out

    def is_destructive(self) -> Tuple[bool, str]:
        """Indique si la configuration autorise une operation destructive.

        Retourne `(est_destructive, raison)`. Trois sources possibles :
        `TABLE_EXISTS_ACTION` destructif, `CLEANUP_ON_FAILURE`, ou
        `REMOTE_TRANSFER` qui ecrase la destination.
        """
        if self.values["TABLE_EXISTS_ACTION"] in DESTRUCTIVE_ACTIONS:
            return True, (
                f"TABLE_EXISTS_ACTION={self.values['TABLE_EXISTS_ACTION']} "
                "modifie des donnees deja presentes dans le schema cible"
            )
        if self.values["CLEANUP_ON_FAILURE"]:
            return True, "CLEANUP_ON_FAILURE=true supprime des artefacts en cas d'echec"
        return False, ""


def load(
    path: Optional[Path] = None,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    env_prefix: str = "OSD_",
) -> Config:
    """Charge la configuration avec superposition defauts < fichier < env < CLI.

    L'ordre compte : les options de la ligne de commande et l'environnement
    d'ordonnancement doivent pouvoir corriger un fichier de configuration
    partage, sans jamais le modifier sur disque.
    """
    raw: Dict[str, str] = {}
    warnings: List[str] = []

    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(
                f"fichier de configuration introuvable : {p}",
                hint="Copier config/config.example.conf vers config/config.conf",
            )
        mode = p.stat().st_mode & 0o777
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{p}: encodage invalide ({exc.reason})") from None
        except OSError as exc:
            raise ConfigError(f"{p}: lecture impossible ({exc.strerror})") from None
        raw.update(parse_conf(text, origin=str(p)))
        if mode & 0o077:
            # Avertissement, pas refus : le fichier ne contient normalement
            # aucun secret (le mot de passe est un repli degrade) et un
            # refus serait bloquant sur un partage de groupe. Le compte
            # rendu est porte par `Config.warnings`.
            warnings.append(f"{p} lisible par d'autres (mode {mode:04o})")

    for key, spec in SCHEMA.items():
        env_key = env_prefix + key
        if env_key in os.environ:
            raw[key] = os.environ[env_key]

    if overrides:
        for key, value in overrides.items():
            if value is None:
                continue
            if key not in SCHEMA:
                raise ConfigError(f"option inconnue : {key}")
            raw[key] = str(value)

    values: Dict[str, Any] = {}
    for key, spec in SCHEMA.items():
        if key not in raw:
            values[key] = _default(spec)
            continue
        text_value = raw[key]
        if text_value == "" and spec.default not in ("", None):
            # Cle presente mais vide, alors qu'un defaut existe : c'est
            # presque toujours une ligne oubliee plutot qu'une intention
            # de vider la valeur. Le defaut gagne, et l'incoherence est
            # signalee — un operateur qui ecrit `PARALLEL=` veut une
            # valeur, pas un defaut.
            values[key] = _default(spec)
            warnings.append(
                f"{key} renseigne a vide, defaut applique ({spec.default!r})"
            )
            continue
        values[key] = _coerce(key, text_value, spec)

    cfg = Config(values=values, source=Path(path) if path else None,
                 overrides=dict(overrides or {}), warnings=warnings)
    validate(cfg, warnings=warnings)
    return cfg


def validate(cfg: Config, *, warnings: Optional[List[str]] = None) -> None:
    """Verifie les invariants metier, independamment du typage.

    Les anomalies non bloquantes sont ajoutees a `warnings` si une liste
    est fournie. La liste est un parametre et non un attribut de `Config`
    parce que la validation doit pouvoir etre appelee seule, sur une
    configuration deja construite, sans effet de bord.
    """
    if warnings is None:
        warnings = cfg.warnings
    values = cfg.values

    # -- Coherence source / cible ---------------------------------------
    src = validate_identifier(values.get("SOURCE_SCHEMA", ""), "SOURCE_SCHEMA")
    tgt = validate_identifier(values.get("TARGET_SCHEMA", ""), "TARGET_SCHEMA")
    # `OS_AUTH` dispense de `CONNECT`, et l'exclut. Mesure sur les hotes :
    # `/ as sysdba` repond, `/@alias as sysdba` est refuse en ORA-01017.
    # L'authentification OS est resolue par le **systeme**, sur la machine
    # ou s'execute le client : une chaine (base distante) ou un wallet
    # (identite externe) sont donc deux formes de connexion contradictoires
    # avec elle, et une combinaison ne peut aboutir qu'a un ORA-01017
    # refuse apres coup, a l'etape qui ne le lit que comme
    # « authentification ».
    for prefix in ("SOURCE", "TARGET"):
        connexion = f"{prefix}_CONNECT"
        os_auth = bool(values.get(f"{prefix}_OS_AUTH"))
        if os_auth:
            for cle in (connexion, f"{prefix}_WALLET"):
                if values.get(cle):
                    raise ConfigError(
                        f"{prefix}_OS_AUTH=true est incompatible avec {cle}",
                        hint=f"{cle} designe une base distante ou un "
                             f"wallet, alors que l'authentification OS est "
                             f"locale : elle passe par `/`, l'identite "
                             f"du compte d'exploitation. Retirer {cle}, "
                             f"ou poser {prefix}_OS_AUTH=false et "
                             f"renseigner {connexion} (et {prefix}_WALLET "
                             "si besoin).",
                    )
        elif not values.get(connexion):
            raise ConfigError(f"{connexion} absent")
    if not values.get("SOURCE_DIRECTORY"):
        raise ConfigError("SOURCE_DIRECTORY absent")
    if not values.get("TARGET_DIRECTORY"):
        raise ConfigError("TARGET_DIRECTORY absent")

    if src == tgt and not values.get("ALLOW_EXISTING_TARGET"):
        raise ConfigError(
            f"SOURCE_SCHEMA et TARGET_SCHEMA sont identiques ({src})",
            hint="Une duplication vers le meme schema est presque toujours "
                 "une erreur de configuration.",
        )

    for key, label in (("SOURCE_DIRECTORY", "SOURCE_DIRECTORY"),
                       ("TARGET_DIRECTORY", "TARGET_DIRECTORY")):
        if not _OBJECT_NAME_RE.match(values[key]):
            raise ConfigError(f"{label} invalide : {values[key]!r}")

    # -- Compressibilite : Oracle refuse une compression sans donnees ----
    if values["CONTENT"] == "DATA_ONLY" and values["COMPRESSION"] == "METADATA_ONLY":
        raise ConfigError(
            "COMPRESSION=METADATA_ONLY est incoherent avec CONTENT=DATA_ONLY",
            hint="Utiliser COMPRESSION=ALL ou DATA_ONLY.",
        )

    # -- Securite : le mot de passe en clair n'est accepte qu'en repli ----
    if cfg.has_password and not values.get("SOURCE_WALLET") and not values.get("TARGET_WALLET"):
        raise ConfigError(
            "PASSWORD est defini sans wallet configure",
            hint="Utiliser un Oracle Wallet (SOURCE_WALLET / TARGET_WALLET). "
                 "Le mot de passe en clair est un repli degrade : il "
                 "reapparait dans les arguments de processus et dans les "
                 "journaux Data Pump.",
        )

    # -- Coherence des options d'execution distante ----------------------
    # L'authentification SSH n'est plus une option de ce fichier : elle
    # vit dans l'inventaire Ansible, chiffree par Vault. Les six cles
    # qui la portaient sont donc refusees des qu'elles portent une
    # valeur -- une option acceptee et ignoree est pire qu'une option
    # refusee, car elle laisse croire a un comportement qui n'existe pas.
    for key in _OBSOLETE_SSH_KEYS:
        if values.get(key):
            raise ConfigError(
                f"{key} n'est plus pris en charge",
                hint="L'authentification SSH se declare dans "
                     "l'inventaire Ansible, chiffre par Vault. Retirer "
                     f"{key} de la configuration.",
            )
    remote_sides = [
        prefix for prefix in ("SOURCE", "TARGET") if values.get(f"{prefix}_HOST")
    ]
    # Un cote local et un cote distant n'est pas une topologie supportee.
    # `expdp` ecrit alors le dump sur le serveur de saut, `impdp` le lit
    # sur l'hote distant qui ne voit pas ce systeme de fichiers, et la
    # couche de transfert ne sait pas remplacer une source distante par
    # un chemin local. L'import echouerait donc sur un dump valide, trois
    # etapes plus loin. Refuser ici, a l'etape 2, plutot que laisser
    # l'echec surfaire a l'etape 14 sans lien avec sa cause.
    if len(remote_sides) == 1:
        renseigne = remote_sides[0]
        manquant = "TARGET" if renseigne == "SOURCE" else "SOURCE"
        # Les deux noms sont dans le message : la cle a **remplir** est
        # celle qu'il faut designer. « SOURCE_HOST est renseigne mais
        # l'autre cote ne l'est pas » laisse le lecteur chercher lequel des
        # deux, et l'installation d'un outil n'a pas vocation a devenir
        # une devinette.
        raise ConfigError(
            f"{renseigne}_HOST est renseigne mais {manquant}_HOST est vide",
            hint="Le transfert du dump n'est implemente qu'entre deux "
                 f"hotes distants : renseigner {manquant}_HOST, ou vider les "
                 f"deux pour une execution integrale sur le serveur de saut.",
        )
    # Inventaire Ansible : seule voie d'acces aux hotes distants.
    if remote_sides:
        inventory = values.get("OSD_INVENTORY", "")
        if not inventory:
            raise ConfigError(
                "OSD_INVENTORY absent alors qu'un hote distant est designe",
                hint="Renseigner OSD_INVENTORY, ou vider SOURCE_HOST et "
                     "TARGET_HOST pour une execution locale.",
            )
        path = Path(inventory)
        if not path.is_file():
            raise ConfigError(
                f"OSD_INVENTORY introuvable : {inventory}",
                hint="Le chemin est relatif au repertoire d'execution de "
                     "`osd`, donc fragile sous cron : preferer un chemin "
                     "absolu.",
            )
        vault = values.get("OSD_VAULT_PASSWORD_FILE", "")
        if not vault:
            raise ConfigError(
                "OSD_VAULT_PASSWORD_FILE absent alors qu'un hote distant "
                "est designe",
                hint="Sans fichier de mot de passe de coffre, Ansible ne peut "
                     "pas dechiffrer les mots de passe de l'inventaire. Le "
                     "fichier doit etre hors du depot, en 0600.",
            )
        vpath = Path(vault)
        if not vpath.is_file():
            raise ConfigError(
                f"OSD_VAULT_PASSWORD_FILE introuvable : {vault}",
                hint="Le fichier doit exister et etre lisible par le compte "
                     "qui execute.",
            )
        mode = vpath.stat().st_mode & 0o777
        if mode & 0o077:
            # Un mot de passe de coffre lisible par d'autres n'est pas un
            # mot de passe de coffre : c'est une annotation. Le dire ici
            # plutot que laisser Ansible echouer en disant autre chose.
            raise ConfigError(
                f"OSD_VAULT_PASSWORD_FILE trop permissive : {vault} "
                f"(mode {mode:04o}, attendu 0600)",
                hint="Corriger par : chmod 600 " + str(vault),
            )

    # -- Parallelisme : au-dela de 1, l'export produit N fichiers ---------
    if values["PARALLEL"] < 1:
        raise ConfigError("PARALLEL doit etre >= 1")
    if values["PARALLEL"] > 64:
        raise ConfigError("PARALLEL doit etre <= 64 (limite Oracle Data Pump)")

    # -- Marge d'espace --------------------------------------------------
    if not 0 <= values["SPACE_MARGIN_PERCENT"] <= 500:
        raise ConfigError("SPACE_MARGIN_PERCENT doit etre dans [0, 500]")
    if values["SPACE_MARGIN_ABS_MB"] < 0:
        raise ConfigError("SPACE_MARGIN_ABS_MB doit etre >= 0")

    # -- Repertoires -----------------------------------------------------
    for key in ("LOG_DIR", "WORK_DIR", "REPORT_DIR"):
        if not values[key]:
            raise ConfigError(f"{key} absent de la configuration")

    # -- Cles obsoletes : refusees plutot que tolerees -------------------
    for key in ("STAGING_DIR", "REMOTE_TRANSFER"):
        if values.get(key) not in ("", False):
            raise ConfigError(
                f"{key} n'est plus pris en charge",
                hint="Le dump transite par le serveur de saut, qui n'a "
                     "besoin d'aucun client Oracle. Retirer cette cle de la "
                     "configuration.",
            )


def describe() -> List[Tuple[str, str, str]]:
    """Retourne `(cle, defaut, doc)` pour la documentation et `--help`."""
    return [(key, str(spec.default), spec.doc) for key, spec in sorted(SCHEMA.items())]


def schema_keys() -> Sequence[str]:
    return tuple(sorted(SCHEMA))
