"""Creation du compte cible a l'image du compte source.

Le compte cible est deduit des metadonnees du compte source : tablespace
par defaut et temporaire, profil, privileges systeme, roles, quota, et
empreinte du mot de passe. La construction est **separee** de l'execution :
`plan_create_target_schema` ne fait que lire et rendre du SQL. C'est ce qui
permet a `check` et au `dry-run` de valider une creation sans jamais
ecrire, et de presenter le DDL qui serait applique.

Le mot de passe n'est jamais en clair : la forme `IDENTIFIED BY VALUES`
reprend l'empreinte du compte source, et le redacteur la masque dans les
journaux et les rapports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import validate_identifier
from ..errors import PrereqError


@dataclass
class CreateSchemaPlan:
    """DDL a appliquer, plus de quoi l'expliquer dans le rapport."""

    statements: List[str] = field(default_factory=list)
    detail: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _lit(value: str) -> str:
    """Echappe une valeur pour un litteral SQL entre quotes simples."""
    return str(value).replace("'", "''")


def _first_row(adapter: Any, sql: str) -> Optional[List[str]]:
    rows = adapter.query(sql)
    return rows[0] if rows else None


def _remap_map(pairs: Sequence[str]) -> Dict[str, str]:
    """Normalise les paires `SOURCE:CIBLE` en table de correspondance."""
    out: Dict[str, str] = {}
    for item in pairs:
        source, target = item.split(":", 1)
        out[source.strip().upper()] = target.strip()
    return out


def plan_create_target_schema(
    source_adapter: Any,
    target_adapter: Any,
    *,
    source_schema: str,
    target_schema: str,
    remap_tablespace: Sequence[str] = (),
) -> CreateSchemaPlan:
    """Prepare le `CREATE USER` du compte cible, sans rien executer.

    Lit le compte source, valide ce que la cible peut recevoir, et rend les
    instructions SQL. Leve une `PrereqError` (code 2) quand la cible ne
    peut pas accueillir le compte tel quel : tablespace par defaut ou
    temporaire absent, compte present mais non ouvert, empreinte illisible.
    Un profil absent de la cible est un simple repli sur `DEFAULT` ; un
    role absent est ignore avec un avertissement.
    """
    src = validate_identifier(source_schema, "SOURCE_SCHEMA")
    tgt = validate_identifier(target_schema, "TARGET_SCHEMA")
    remap = _remap_map(remap_tablespace)

    def mapped(name: str) -> str:
        return remap.get(name.strip().upper(), name.strip())

    # -- Identite du compte source --------------------------------------
    row = _first_row(
        source_adapter,
        "select default_tablespace, temporary_tablespace, profile "
        "from dba_users where username = '%s'" % _lit(src),
    )
    if not row or len(row) < 3:
        raise PrereqError(
            f"le compte source {src} est introuvable : impossible d'en "
            f"deduire le compte cible",
            hint="Verifier SOURCE_SCHEMA. L'etape 6 a pourtant valide le "
                 "schema source : le compte a pu changer de statut depuis.",
        )
    default_ts = mapped(row[0])
    temp_ts = mapped(row[1])
    profile = row[2].strip()

    # -- Empreinte du mot de passe (jamais en clair) --------------------
    verifier = source_adapter.scalar(
        "select spare4 from sys.user$ where name = '%s'" % _lit(src)
    )
    if not verifier:
        raise PrereqError(
            f"empreinte du mot de passe de {src} illisible",
            hint="CREATE_TARGET_SCHEMA reprend l'empreinte du compte source "
                 "(IDENTIFIED BY VALUES), ce qui exige un acces au "
                 "dictionnaire (SYSDBA) sur la source. Accorder cet acces, "
                 "ou creer le compte cible au prealable.",
        )

    # -- Privileges, roles et quota du compte source --------------------
    privs = [
        r[0].strip()
        for r in source_adapter.query(
            "select privilege from dba_sys_privs where grantee = '%s'" % _lit(src)
        )
        if r and r[0].strip()
    ]
    roles = [
        r[0].strip()
        for r in source_adapter.query(
            "select granted_role from dba_role_privs where grantee = '%s'" % _lit(src)
        )
        if r and r[0].strip()
    ]
    quotas = [
        (r[0].strip(), int(r[1]) if len(r) > 1 and r[1].strip() else -1)
        for r in source_adapter.query(
            "select tablespace_name, max_bytes from dba_ts_quotas "
            "where username = '%s'" % _lit(src)
        )
        if r and r[0].strip()
    ]

    # -- Ce que la cible peut recevoir ----------------------------------
    target_ts = {
        r[0].strip().upper()
        for r in target_adapter.query("select tablespace_name from dba_tablespaces")
        if r and r[0].strip()
    }
    target_profiles = {
        r[0].strip().upper()
        for r in target_adapter.query("select distinct profile from dba_profiles")
        if r and r[0].strip()
    }
    target_roles = {
        r[0].strip().upper()
        for r in target_adapter.query("select role from dba_roles")
        if r and r[0].strip()
    }

    plan = CreateSchemaPlan()

    # Un compte present mais non ouvert (LOCKED, EXPIRED) n'est pas un
    # compte absent : tenter un `CREATE USER` echouerait en ORA-01920,
    # apres coup, avec un message qui ne dit pas quoi faire. On le
    # distingue ici, avant toute ecriture.
    existing = _first_row(
        target_adapter,
        "select account_status from dba_users where username = '%s'" % _lit(tgt),
    )
    if existing and existing[0].strip().upper() != "OPEN":
        raise PrereqError(
            f"le compte cible {tgt} existe mais n'est pas ouvert "
            f"({existing[0].strip()})",
            hint="Deverrouiller le compte, ou le recreer, avant de relancer. "
                 "L'outil ne modifie pas le statut d'un compte existant.",
        )

    if default_ts.upper() not in target_ts:
        raise PrereqError(
            f"le tablespace par defaut {default_ts} du compte source n'existe "
            f"pas sur la cible",
            hint="Creer le tablespace sur la cible, ou le rediriger avec "
                 f"REMAP_TABLESPACE={default_ts}:<tablespace cible>.",
        )
    if temp_ts.upper() not in target_ts:
        raise PrereqError(
            f"le tablespace temporaire {temp_ts} du compte source n'existe "
            f"pas sur la cible",
            hint="Creer le tablespace temporaire sur la cible, ou le "
                 f"rediriger avec REMAP_TABLESPACE={temp_ts}:<tablespace cible>.",
        )

    if profile.upper() not in target_profiles:
        plan.warnings.append(
            f"le profil {profile} du compte source n'existe pas sur la cible : "
            f"repli sur DEFAULT"
        )
        profile = "DEFAULT"

    roles_absents = [r for r in roles if r.upper() not in target_roles]
    for role in roles_absents:
        plan.warnings.append(
            f"le role {role} n'existe pas sur la cible : grant ignore"
        )
    roles = [r for r in roles if r.upper() in target_roles]

    # -- Rendu du DDL ---------------------------------------------------
    clauses = [
        f"identified by values '{_lit(verifier)}'",
        f"default tablespace {default_ts}",
        f"temporary tablespace {temp_ts}",
        f"profile {profile}",
    ]
    for name, max_bytes in quotas:
        ts = mapped(name)
        if ts.upper() not in target_ts:
            plan.warnings.append(
                f"quota du tablespace {ts} ignore : absent de la cible"
            )
            continue
        if max_bytes is None or max_bytes < 0:
            clauses.append(f"quota unlimited on {ts}")
        else:
            clauses.append(f"quota {max_bytes} on {ts}")

    plan.statements.append(f"create user {tgt} {' '.join(clauses)}")
    if privs:
        plan.statements.append(f"grant {', '.join(privs)} to {tgt}")
    if roles:
        plan.statements.append(f"grant {', '.join(roles)} to {tgt}")

    plan.detail.append(
        f"compte cible {tgt} derive de {src} : tablespace {default_ts}, "
        f"temporaire {temp_ts}, profil {profile}"
    )
    plan.detail.append(
        f"privileges : {', '.join(privs) or '(aucun)'} ; "
        f"roles : {', '.join(roles) or '(aucun)'}"
    )
    return plan