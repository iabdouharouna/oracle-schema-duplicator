"""Controles prealables, partages par `check` et par `run`.

Chaque controle est une fonction pure qui retourne un `CheckResult`.
Cette forme a une raison precise : un controle doit pouvoir etre execute
et verifie **sans base de donnees ni reseau**. Les tests de AGENTS.md
(schema inexistant, tablespace inexistant, espace insuffisant, privileges
insuffisants) sont alors des tests de caracteres ordinaires, avec un
faux adaptateur en place du vrai.

Le rendu en texte et en JSON est fait par `report/`, jamais ici : un
controle ne connait pas la forme du rapport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .. import exit_codes as ec
from ..adapters.oracle import is_19c
from ..errors import OsdError
from ..logging_setup import get_logger

LOG = get_logger()

#: Severites possibles, dans l'ordre croissant de gravite.
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

_SEVERITY_ORDER = {OK: 0, SKIP: 1, WARN: 2, FAIL: 3}


@dataclass
class CheckResult:
    """Issue d'un controle."""

    name: str
    status: str = OK
    code: int = ec.SUCCESS
    message: str = ""
    detail: List[str] = field(default_factory=list)
    hint: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    @property
    def warning(self) -> bool:
        return self.status == WARN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "detail": list(self.detail),
            "hint": self.hint,
            "data": self.data,
        }

    def to_lines(self) -> List[str]:
        """Rendu texte, sans couleur : lisible sous cron comme dans un mail."""
        mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}[self.status]
        lines = [f"{mark} {self.name}"]
        if self.message:
            lines.append(f"       {self.message}")
        for item in self.detail:
            lines.append(f"         - {item}")
        if self.hint and self.status in (WARN, FAIL):
            lines.append(f"       remed(e) : {self.hint}")
        return lines


def worst(results: Sequence[CheckResult]) -> str:
    """Retourne la severite la plus forte d'une serie de controles."""
    if not results:
        return OK
    return max((r.status for r in results), key=lambda s: _SEVERITY_ORDER.get(s, 0))


def first_failure(results: Sequence[CheckResult]) -> Optional[CheckResult]:
    """Premier controle en echec, dans l'ordre d'execution.

    L'ordre compte : il est celui du workflow, donc le premier echec est
    la cause la plus en amont, celle qu'il faut corriger en premier.
    """
    for result in results:
        if result.failed:
            return result
    return None


# --------------------------------------------------------------------------
# Etape 3 — dependances
# --------------------------------------------------------------------------

def check_dependencies(source_runner, target_runner) -> List[CheckResult]:
    """Verifie la presence des outils sur chaque cote.

    Les outils ne sont pas les memes selon l'execution : en local, tout
    est sur la meme machine ; en distant, `expdp` doit etre sur l'hote
    source et `impdp` sur l'hote cible, ce qui n'est vrai ni de la meme
    facon ni sur le meme serveur.
    """
    results: List[CheckResult] = []

    required = [
        ("source", source_runner, ("expdp", "sqlplus"), ec.PREREQ),
        ("target", target_runner, ("impdp", "sqlplus"), ec.PREREQ),
    ]
    for label, runner, tools, code in required:
        missing: List[str] = []
        for tool in tools:
            if not runner.has_binary(tool):
                missing.append(tool)
        if missing:
            results.append(
                CheckResult(
                    name=f"dependances {label}",
                    status=FAIL,
                    code=code,
                    message=f"absent(s) sur {runner.label} : {', '.join(missing)}",
                    hint="Verifier le PATH du compte execute, ou installer "
                         "le client Oracle 19c sur cet hote.",
                )
            )
        else:
            results.append(
                CheckResult(
                    name=f"dependances {label}",
                    status=OK,
                    message=f"expdp/impdp/sqlplus presents sur {runner.label}",
                )
            )
    return results


# --------------------------------------------------------------------------
# Etapes 4 et 5 — connexions
# --------------------------------------------------------------------------

def check_connection(label: str, adapter) -> CheckResult:
    """Verifie la connexion a un cote et collecte ses metadonnees."""
    name = f"connexion {label}"
    try:
        info = adapter.check_connection()
    except OsdError as exc:
        return CheckResult(
            name=name,
            status=FAIL,
            code=exc.code,
            message=exc.message,
            detail=list(exc.detail),
            hint=exc.hint,
        )

    results = [CheckResult(name=name, status=OK, data=info)]

    status = (info.get("status") or "").upper()
    if status and status not in ("OPEN", "OPEN READ ONLY"):
        results.append(
            CheckResult(
                name=f"instance {label}",
                status=FAIL,
                code=ec.PREREQ,
                message=f"instance {info.get('instance', '?')} en etat {status}",
                hint="Une duplication vers une instance fermee, en cours de "
                     "redemarrage ou en montage echouerait a l'import.",
            )
        )
    else:
        results.append(
            CheckResult(
                name=f"instance {label}",
                status=OK,
                message=f"{info.get('instance', '?')} sur {info.get('host', '?')}, "
                        f"etat {status or 'inconnu'}",
            )
        )

    # Seule la version **majeure** est comparee : 19.0, 19.3 et 19.20
    # sont tous 19c. Comparer la chaine entiere ferait echouer la
    # validation sur une base 19c appliquee par RUs — c'est-a-dire dans
    # la majorite des installations de production.
    version = info.get("version", "")
    if version and not is_19c(version):
        results.append(
            CheckResult(
                name=f"version {label}",
                status=WARN,
                code=ec.PREREQ,
                message=f"version {version} differente de 19c",
                hint="Outil concu pour 19c. Un dump 19c ne peut pas etre "
                     "importe dans une base plus ancienne ; viser une base "
                     "plus recente est en general possible.",
            )
        )
    else:
        results.append(
            CheckResult(
                name=f"version {label}",
                status=OK,
                message=f"Oracle {version or 'inconnue'}",
            )
        )
    return _merge(name, results)


# --------------------------------------------------------------------------
# Etapes 6 a 8 — schema et tablespaces
# --------------------------------------------------------------------------

def check_source_schema(adapter, schema: str, *, content: str) -> CheckResult:
    """Verifie que le schema source existe et n'est pas vide.

    Un schema vide n'est pas une erreur technique : c'est presque
    toujours une erreur de frappe dans la configuration. Le message le dit
    explicitement plutot que de laisser un export produire un dump vide,
    dont l'import reussirait sans rien creer.
    """
    name = f"schema source {schema}"
    if not adapter.schema_exists(schema):
        return CheckResult(
            name=name,
            status=FAIL,
            code=ec.PREREQ,
            message=f"le schema {schema} n'existe pas ou n'est pas ouvert",
            hint=f"Verifier le nom (attention a la casse) et le statut du "
                 f"compte. Les schemas disponibles se=listent par : "
                 f"select username from dba_users where account_status='OPEN'",
        )

    objects = adapter.object_count(schema)
    if objects == 0:
        return CheckResult(
            name=name,
            status=FAIL,
            code=ec.PREREQ,
            message=f"le schema {schema} existe mais ne contient aucun objet",
            hint="Un export d'un schema vide produirait un dump vide dont "
                 "l'import reussirait sans rien creer. Verifier que le "
                 "schema attendu est le bon.",
        )

    invalid = adapter.object_count(schema, object_type="INVALID")
    if invalid:
        return CheckResult(
            name=name,
            status=WARN,
            code=ec.PREREQ,
            message=f"{invalid} objet(s) invalide(s) dans {schema}",
            hint="Data Pump exporte un objet invalide sans echouer, mais il "
                 "sera recree invalide cote cible. Corriger la source si "
                 "l'integrite attendue fait partie du besoin.",
            data={"objects": objects, "invalid": invalid},
        )
    return CheckResult(
        name=name,
        status=OK,
        message=f"{objects} objet(s)",
        data={"objects": objects},
    )


def check_target_schema(adapter, schema: str, *, allow_existing: bool) -> CheckResult:
    """Verifie l'etat du schema cible.

    Un schema cible deja peuple est refuse par defaut : `SKIP` laisserait
    un melange de donnees anciennes et nouvelles, et `REPLACE` ecraserait
    des donnees sans que l'exploitant l'ait demande. C'est la raison
    d'etre de `ALLOW_EXISTING_TARGET`, et la raison du controle.
    """
    name = f"schema cible {schema}"
    if not adapter.schema_exists(schema):
        return CheckResult(
            name=name,
            status=FAIL,
            code=ec.PREREQ,
            message=f"le schema {schema} n'existe pas",
            hint="L'outil ne cree pas de schema : la creation du compte cible "
                 "relève de l'initialisation de la base, pas de la "
                 "duplication. Verifier le nom et le statut du compte.",
        )

    objects = adapter.object_count(schema)
    if objects == 0:
        return CheckResult(
            name=name,
            status=OK,
            message=f"{schema} existe et est vide",
            data={"objects": 0},
        )

    if not allow_existing:
        return CheckResult(
            name=name,
            status=FAIL,
            code=ec.SECURITY,
            message=f"{schema} contient deja {objects} objet(s)",
            hint="Refus par conception : ecraser un schema cible n'est "
                 "jamais implicite. Pour assumer explicitement l'ecrasement, "
                 "utiliser ALLOW_EXISTING_TARGET=true avec une "
                 "TABLE_EXISTS_ACTION non destructive (SKIP ou APPEND), ou "
                 "--allow-destructive avec REPLACE.",
            data={"objects": objects},
        )

    return CheckResult(
        name=name,
        status=WARN,
        code=ec.PREREQ,
        message=f"{schema} contient deja {objects} objet(s) (ecrasement assume)",
        hint="Verifier que TABLE_EXISTS_ACTION correspond a l'intention : "
             "SKIP conserve l'existant, APPEND ajoute, REPLACE ecrase.",
        data={"objects": objects},
    )


def check_tablespaces(
    adapter,
    label: str,
    required: Sequence[str],
    *,
    content: str,
    remap: Sequence[str] = (),
) -> List[CheckResult]:
    """Verifie l'existence des tablespaces et mesure la place libre.

    Les tablespaces demandes sont ceux de la cible apres remap : c'est
    eux qui doivent exister et avoir la place. Le remap est donc resolu
    avant l'appel, pas pendant.
    """
    name = f"tablespaces {label}"
    if not required:
        return [
            CheckResult(
                name=name,
                status=SKIP,
                message="aucun tablespace impose par la configuration",
                hint="Les tablespaces seront decouverts a l'import, depuis "
                     "le dump. Utiliser REMAP_TABLESPACE pour les controler.",
            )
        ]

    capacity = adapter.tablespaces_capacity(required)
    results: List[CheckResult] = []
    missing: List[str] = []
    for ts in required:
        if ts.upper() not in capacity:
            missing.append(ts)

    if missing:
        results.append(
            CheckResult(
                name=name,
                status=FAIL,
                code=ec.PREREQ,
                message=f"tablespace(s) inexistant(s) : {', '.join(missing)}",
                hint="Verifier REMAP_TABLESPACE et l'existence des "
                     "tablespaces sur ce cote. L'outil ne les cree pas.",
                data={"missing": missing},
            )
        )
        return results

    results.append(
        CheckResult(
            name=name,
            status=OK,
            message=f"presents : {', '.join(required)}",
            data={ts: capacity[ts.upper()] for ts in required if ts.upper() in capacity},
        )
    )
    return results


#: Les droits Data Pump sont des **roles** (`EXP_FULL_DATABASE`), et
#: `SESSION_PRIVS` les restitue sous deux graphies : le nom court du role,
#: documente, et son developpe, `EXPORT FULL DATABASE` — ce qu'Oracle
#: ecrit reellement. Comparer une seule des deux rendait le controle
#: negatif sur une session pourtant pleinement dotee ; observe sur une
#: vraie base 19c, et invisible pour un jeu de tests qui renvoie
#: l'identique dans les deux sens.
GRANT_EXPORT = ("EXP_FULL_DATABASE", "EXPORT FULL DATABASE")
GRANT_IMPORT = ("IMP_FULL_DATABASE", "IMPORT FULL DATABASE")
#: Privilege objet, et non role : la graphie avec espaces est ici la
#: seule reellement rencontree, celle avec tiret bas est la forme
#: documentee. Les deux sont acceptees par principe.
GRANT_DIRECTORY = ("READ,WRITE ON DIRECTORY", "READ,WRITE_ON_DIRECTORY")
GRANTS_DATAPUMP = GRANT_EXPORT + GRANT_IMPORT + GRANT_DIRECTORY


def check_privileges(adapter, label: str, *, for_export: bool) -> CheckResult:
    """Verifie que le compte connecte a les privileges Data Pump requis.

    Le controle porte sur `SESSION_PRIVS`, c'est-a-dire sur les
    privileges **de la session courante**, privileges de roles compris.
    Un role accorde comme `RESOURCE` ne suffit pas, et l'erreur d'Data
    Pump dans ce cas est peu parlante — d'ou un controle prealable.

    `SESSION_PRIVS` et non `DBA_SESSION_PRIVS` : la question posee est
    « ce compte connecte peut-il dupliquer ? », et seule la session
    courante y repond. Lire le dictionnaire entier poserait deux
    problemes. D'abord l'acces : `DBA_SESSION_PRIVS` demande
    `SELECT ANY DICTIONARY`, que le compte d'exploitation n'a
   forcement pas — le controle echouerait donc sur une base ou
    l'operation, elle, reussit. Ensuite la justesse : le dictionnaire
    contient les privileges de **tous** les comptes, donc la reponse
    pouvait venir d'un tiers. Le cas n'est pas theorique : une base
    19c peut avoir cette vue manquante, et le controle rendait alors un
    echec sur un run parfaitement valide.

    Les graphies sont listees dans `GRANTS_DATAPUMP` plutot que
    normalisees a la volee : `EXPORT FULL DATABASE` ne devient pas
    `EXP_FULL_DATABASE` en remplacant les espaces — le mot change
    aussi. Lister les deux formes rend le controle tolerant sans
    exiger du lecteur de deviner laquelle Oracle a choisie.
    """
    name = f"privileges {label}"
    # Le privilege attendu depend du sens de l'operation : un compte
    # d'export n'a pas automatiquement les droits d'import. Les
    # confondre produirait un avertissement sur un run valide et
    # laisserait passer l'import sans les droits necessaires.
    wanted = "EXP_FULL_DATABASE" if for_export else "IMP_FULL_DATABASE"
    attendus = GRANT_EXPORT if for_export else GRANT_IMPORT
    # Constantes du module, jamais donnees d'execution : la construction
    # par formatage ne laisse aucune place a une injection.
    valeurs = ", ".join(f"'{g}'" for g in GRANTS_DATAPUMP)
    sql = f"select privilege from session_privs where privilege in ({valeurs})"
    try:
        grants = {row[0].strip().upper() for row in adapter.query(sql) if row}
    except OsdError as exc:
        return CheckResult(
            name=name,
            status=FAIL,
            code=exc.code,
            message=f"impossible de lire les privileges ({exc.message})",
            hint=f"La vue SESSION_PRIVS est en principe accessible a tout "
                 f"compte : son absence indique une base incomplete ou une "
                 f"session etrangere (proxy). Verifier manuellement avec "
                 f"\"SELECT privilege FROM session_privs\" sous le compte "
                 f"reellement utilise, puis accorder {wanted}.",
        )

    if grants.intersection(attendus):
        return CheckResult(
            name=name,
            status=OK,
            message=f"{wanted} accorde",
            data={"grants": sorted(grants)},
        )
    if grants.intersection(GRANT_DIRECTORY):
        return CheckResult(
            name=name,
            status=WARN,
            code=ec.PREREQ,
            message="READ,WRITE ON DIRECTORY uniquement",
            hint=f"Pour une duplication de schema, {wanted} est "
                 "normalement requis. Avec un simple acces au DIRECTORY, "
                 "l'export est limite au schema du compte connecte.",
            data={"grants": sorted(grants)},
        )
    return CheckResult(
        name=name,
        status=FAIL,
        code=ec.PREREQ,
        message=f"ni {wanted} ni acces au DIRECTORY",
        hint=f"Accorder {wanted} au compte connecte, ou au moins "
             f"READ,WRITE ON DIRECTORY sur l'objet DIRECTORY utilise.",
        data={"grants": sorted(grants)},
    )


# --------------------------------------------------------------------------
# Etape 9 — espace
# --------------------------------------------------------------------------

def check_space(
    adapter,
    label: str,
    directory_name: str,
    directory_path: str,
    *,
    required_bytes: int,
    margin_percent: int,
    margin_abs_bytes: int,
    min_free_bytes: int = 0,
    remote_free_bytes: Optional[int] = None,
    content: str = "ALL",
) -> List[CheckResult]:
    """Verifie la place disponible, sur deux frontiers distinctes.

    Deux espaces sont concernes, et ils se comportent differemment :

    * **le disque du serveur**, qui heberge le dump pendant l'export et
      pendant le transfert. Il est mesure par `remote_space.sh`, donc
      hors SQL. Un export echoue ici si le disque est plein, apres avoir
      deja consommé du temps et du volume reseau.
    * **le tablespace cible**, qui recoit les donnees apres import. Il
      est mesure par `DBA_FREE_SPACE`, donc en SQL. Un import echoue ici
      apres avoir deja transfere le dump.

    Verifier les deux avant d'agir est la seule facon d'echouer tot : le
    cout d'un echec tardif (transfert de plusieurs giga-octets pour rien)
    est disproportionne.

    L'estimation depend du contenu demande, consequence directe du choix
    de rendre CONTENT configurable : en METADATA_ONLY, le dump ne
    contient pas les donnees, et exiger la place des segments serait une
    erreur qui ferait echouer une operation parfaitement faisable.
    """
    results: List[CheckResult] = []
    name_disk = f"espace disque {label}"
    name_ts = f"espace tablespace {label}"

    # `required_bytes` est **deja** une estimation de la taille du dump,
    #la reduction operee par l'appelant : le pipeline y applique un
    # coefficient distinct pour `CONTENT=ALL` et pour
    # `CONTENT=METADATA_ONLY`, ou le dump ne porte que le DDL. Cette
    # fonction ne le rediscount donc pas — un second coefficient place
    # ici serait multiplicatif avec celui du pipeline, et le facteur
    # reel deviendrait invisible.
    #
    # `basis` n'est qu'un libelle : il indique a l'exploitant **sur quoi**
    # repose le chiffre, ce qui est la seule chose qui le rende
    # verifiable. Une estimation dont on ne peut pas dire d'ou elle sort
    # ne peut pas etre discutee.
    need = required_bytes
    basis = "metadonnees seules" if content == "METADATA_ONLY" else "segments du schema"

    needed_with_margin = need + (need * margin_percent) // 100 + margin_abs_bytes
    needed_with_margin = max(needed_with_margin, min_free_bytes)

    if remote_free_bytes is None:
        results.append(
            CheckResult(
                name=name_disk,
                status=SKIP,
                message="espace disque non mesure (acces systeme de fichiers indisponible)",
                hint="Le dump est ecrit par le serveur Oracle dans le "
                     "répertoire du DIRECTORY. Sans mesure, une erreur "
                     "« espace insuffisant » peut survenir a l'export.",
            )
        )
    else:
        if remote_free_bytes >= needed_with_margin:
            results.append(
                CheckResult(
                    name=name_disk,
                    status=OK,
                    message=f"{human(remote_free_bytes)} libres pour "
                            f"{human(needed_with_margin)} necessaires "
                            f"(estimation : {basis})",
                    data={"free": remote_free_bytes, "needed": needed_with_margin},
                )
            )
        else:
            shortfall = needed_with_margin - remote_free_bytes
            results.append(
                CheckResult(
                    name=name_disk,
                    status=FAIL,
                    code=ec.PREREQ,
                    message=f"espace insuffisant : {human(remote_free_bytes)} "
                            f"libres, {human(needed_with_margin)} necessaires "
                            f"(manque {human(shortfall)})",
                    hint="Liberer de l'espace sur le systeme de fichiers "
                         "portant le repertoire du DIRECTORY, ou etendre le "
                         "tablespace, ou utiliser CONTENT=METADATA_ONLY. "
                         "La marge de securite est parametrable "
                         "(SPACE_MARGIN_PERCENT, SPACE_MARGIN_ABS_MB).",
                    data={"free": remote_free_bytes, "needed": needed_with_margin,
                          "shortfall": shortfall},
                )
            )

    # -- Espace du tablespace cible ---------------------------------------
    if content == "METADATA_ONLY":
        results.append(
            CheckResult(
                name=name_ts,
                status=SKIP,
                message="non pertinent en CONTENT=METADATA_ONLY",
                hint="Aucune donnee n'est transportee : les segments du "
                     "tablespace cible ne sont pas dimensionnes.",
            )
        )
        return results

    ts_results = check_tablespace_capacity(
        adapter, label, directory_name, need,
        margin_percent=margin_percent, margin_abs_bytes=margin_abs_bytes,
    )
    results.extend(ts_results)
    return results


def check_tablespace_capacity(
    adapter,
    label: str,
    directory_name: str,
    need_bytes: int,
    *,
    margin_percent: int,
    margin_abs_bytes: int,
) -> List[CheckResult]:
    """Verifie la place libre dans les tablespaces de donnees de la cible.

    Le controle porte sur l'ensemble des tablespaces permanents et non sur
    un nom unique : les donnees peuvent etre redistribuees par le remap,
    et exiger la totalite dans un seul tablespace serait faux. La marge
    est la meme que pour le disque, afin que les deux verdicts soient
    comparables dans le rapport.
    """
    name = f"espace tablespace {label}"
    try:
        capacity = adapter.tablespaces_capacity(_candidate_tablespaces(adapter))
    except OsdError as exc:
        return [
            CheckResult(
                name=name,
                status=FAIL,
                code=exc.code,
                message=f"mesure impossible ({exc.message})",
                hint="Le compte connecte a probablement pas acces a "
                     "DBA_FREE_SPACE / DBA_DATA_FILES.",
            )
        ]

    total_free = sum(v["free_bytes"] for v in capacity.values())
    needed = need_bytes + (need_bytes * margin_percent) // 100 + margin_abs_bytes

    if total_free <= 0:
        return [
            CheckResult(
                name=name,
                status=WARN,
                code=ec.PREREQ,
                message="place libre non mesurable (DBA_FREE_SPACE vide ou absent)",
                hint="L'import peut echouer sur place insuffisante. Verifier "
                     "la place dans les tablespaces de la cible avant de "
                     "lancer un import volumineux.",
            )
        ]

    if total_free >= needed:
        return [
            CheckResult(
                name=name,
                status=OK,
                message=f"{human(total_free)} libres au total pour "
                        f"{human(needed)} necessaires",
                data={"free": total_free, "needed": needed,
                      "tablespaces": capacity},
            )
        ]
    return [
        CheckResult(
            name=name,
            status=FAIL,
            code=ec.PREREQ,
            message=f"place insuffisante : {human(total_free)} libres, "
                    f"{human(needed)} necessaires",
            hint="Etendre un tablespace de la cible avant l'import. Le "
                 "refus est anticipe ici plutot que dans l'import, ou il "
                 "laisserait un schema partiellement peuple.",
            data={"free": total_free, "needed": needed,
                  "shortfall": needed - total_free},
        )
    ]


def _candidate_tablespaces(adapter) -> List[str]:
    """Liste les tablespaces permanents de la base, vus de l'utilisateur.

    La liste est obtenue dynamiquement plutot que codee en dur : les
    noms `USERS` ou `DATA` varient d'une installation a l'autre.
    """
    try:
        rows = adapter.query(
            "select distinct tablespace_name from dba_data_files order by 1"
        )
    except OsdError:
        return []
    return [row[0].strip() for row in rows if row and row[0].strip()]


# --------------------------------------------------------------------------
# Regroupement
# --------------------------------------------------------------------------

def _merge(base: str, results: Sequence[CheckResult]) -> CheckResult:
    """Fusionne plusieurs controles liees en un seul resultat.

    Le controle le plus grave l'emporte ; les autres sont conserves dans
    `detail` pour ne rien perdre de l'information.
    """
    if len(results) == 1:
        return results[0]
    top = max(results, key=lambda r: _SEVERITY_ORDER.get(r.status, 0))
    merged = CheckResult(
        name=base,
        status=top.status,
        code=top.code,
        message=top.message,
        hint=top.hint,
        data=top.data,
    )
    for result in results:
        if result is not top:
            label = result.name.split(" ", 1)[-1]
            merged.detail.append(f"{label}: {result.message}")
    return merged


def human(n: int) -> str:
    """Formate une taille, independamment de la locale du serveur de saut."""
    from ..adapters.transfer import human_bytes

    return human_bytes(int(n))
