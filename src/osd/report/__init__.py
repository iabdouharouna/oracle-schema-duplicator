"""Rendu du rapport d'execution : texte pour l'humain, JSON pour la machine.

Deux sorties, deux usages distincts, et la separation est volontaire :

* le **texte** est lu par un exploitant, souvent dans une notification
  cron ou un ticket. Il doit tenir en un ecran, dire ce qui s'est passe
  et ce qu'il faut faire, sans jargon ;
* le **JSON** est lu par une supervision ou une chaine CI. Il doit etre
  stable, donc versionne, et ne contenir aucun secret.

Le JSON n'est jamais produit par serialisation directe de l'objet
`State` : il passe par `build_document`, qui choisit explicitement les
champs exposes. Cela evite qu'un champ ajoute par commodite dans
l'etat interne se retrouve publier dans le rapport.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .. import exit_codes as ec
from ..checks.preflight import FAIL, OK, SKIP, WARN, CheckResult, human
from ..redact import redact

#: Version du format JSON. Le suivi permet a une supervision de detecter
#: qu'elle parle a une version differente de l'outil.
REPORT_VERSION = 1

_STATUS_MARK = {OK: "OK", WARN: "WARN", FAIL: "ECHEC", SKIP: "N/A"}


def build_document(
    *,
    cfg,
    state,
    checks: Sequence[CheckResult],
    started_at: float,
    finished_at: float,
    error: Optional[Dict[str, Any]] = None,
    simulated: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Construit le document de rapport.

    `cfg` est passe pour ses seuls champs retenus, et `state` pour son
    etat serialise deja reduit aux cles utiles. Le choix des cles est
    explicite : c'est ce qui rend le format publie stable.

    `simulated` liste les mutations retenues par le `NullRunner` en
    dry-run. La section n'est presente que dans ce cas, et jamais a vide :
    un rapport de dry-run sans cette liste ne dit pas ce qu'il a
    الضوء de ne pas avoir fait, et n'est donc pas relisible. Son absence
    en mode reel signifie au contraire, de maniere explicite, que tout a
    ete reellement execute.
    """
    code = state.final_code if state.final_code is not None else ec.SUCCESS
    doc: Dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "tool_version": _tool_version(),
        "run_id": state.run_id,
        "outcome": {
            "code": code,
            "label": ec.label(code),
            "success": code == ec.SUCCESS,
        },
        "timing": {
            "started_at": _iso(started_at),
            "finished_at": _iso(finished_at),
            "duration_s": round(finished_at - started_at, 3),
        },
        "duplication": {
            "source": state.source,
            "source_schema": state.source_schema,
            "target": state.target,
            "target_schema": state.target_schema,
            "dry_run": state.dry_run,
        },
        "options": _safe_options(cfg),
        "steps": [
            {
                "index": s.index,
                "name": s.name,
                "status": s.status,
                "code": s.code,
                "message": s.message,
                "duration_s": s.duration_s,
                "detail": s.detail,
                # Reproduit champ par champ : l'oublier ici ferait
                # passer une etape reprise pour une etape executee,
                # puisque le rendu teste ce drapeau. Le document est
                # aussi la sortie de `--json`, ou la distinction doit
                # etre lisible sans passer par le texte.
                "carried_over": s.carried_over,
            }
            for _, s in sorted(state.steps.items())
        ],
        "checks": [c.to_dict() for c in checks],
        "artifacts": _artifacts(state),
        "metrics": state.metrics,
        "error": error,
    }
    if state.dry_run:
        doc["simulated"] = {
            "mutations_withheld": list(simulated or ()),
            "count": len(list(simulated or ())),
            "note": (
                "Aucune mutation n'a ete executee. Les lectures — "
                "connexion, schemas, tablespaces, espace — ont en revanche "
                "ete reellement executees : leurs verdicts sont fiables."
            ),
        }
    return doc


def _safe_options(cfg) -> Dict[str, Any]:
    """Options publiees dans le rapport, sans secret.

    Seules les options qui influent sur le **resultat** sont publiees :
    un rapport sert a reproduire ou a comprendre une duplication, pas a
    inventorier le fichier de configuration.
    """
    published = [
        "CONTENT", "COMPRESSION", "PARALLEL", "REMAP_TABLESPACE",
        "EXCLUDE", "INCLUDE", "TABLE_EXISTS_ACTION", "VALIDATION_LEVEL",
        "ALLOW_EXISTING_TARGET", "TRANSFER_MODE",
    ]
    out: Dict[str, Any] = {}
    for key in published:
        if cfg is None:
            break
        out[key] = redact(str(cfg.get(key, "")))
    return out


def _artifacts(state) -> Dict[str, Any]:
    """Artefacts produits, resumes pour le rapport.

    Les empreintes `hashlib` des parties du dump sont conservees : elles
    permettent de verifier plus tard qu'un dump donne dans un ticket est
    bien celui qui a ete importe. Aucun contenu de fichier n'est publie.
    """
    return {
        key: value
        for key, value in state.artifacts.items()
        if not key.endswith("_content")
    }


# --------------------------------------------------------------------------
# Rendu texte
# --------------------------------------------------------------------------

def render_text(doc: Dict[str, Any], *, verbose: bool = False) -> str:
    """Rend le rapport texte, concu pour un ecran.

    La structure est un entete (quoi, quand, combien), un verdict, les
    controles, puis les etapes. Le detail n'est ajoute qu'en mode verbose
    : en exploitation non interactive, un rapport de 400 lignes n'est pas
    lu, et l'essentiel s'y noie.
    """
    out: List[str] = []
    dup = doc["duplication"]
    out.append("=" * 72)
    out.append("Oracle Schema Duplicator — rapport d'execution")
    out.append("=" * 72)
    out.append(f"run         : {doc['run_id']}")
    out.append(f"debut       : {doc['timing']['started_at']}")
    out.append(f"duree       : {human_seconds(doc['timing']['duration_s'])}")
    if dup["dry_run"]:
        out.append("mode        : SIMULATION (aucune ecriture en base)")
    out.append(f"source      : {dup['source']} / schema {dup['source_schema']}")
    out.append(f"cible       : {dup['target']} / schema {dup['target_schema']}")
    out.append("")

    outcome = doc["outcome"]
    verdict = "SUCCES" if outcome["success"] else f"ECHEC ({outcome['code']})"
    out.append(f"VERDICT     : {verdict} — {outcome['label']}")
    out.append("")

    checks = doc.get("checks") or []
    if checks:
        out.append("-" * 72)
        out.append("Controles prealables")
        out.append("-" * 72)
        for check in checks:
            for line in _check_lines(check, verbose=verbose):
                out.append(line)
        out.append("")

    steps = doc.get("steps") or []
    if steps:
        out.append("-" * 72)
        out.append("Etapes")
        out.append("-" * 72)
        for step in steps:
            out.append(_step_line(step))
        out.append("")

    artifacts = doc.get("artifacts") or {}
    if artifacts:
        out.append("-" * 72)
        out.append("Artefacts")
        out.append("-" * 72)
        for line in _artifact_lines(artifacts):
            out.append(f"  {line}")
        out.append("")

    simulated = doc.get("simulated")
    if simulated:
        out.append("-" * 72)
        out.append("Simulation — mutations retenues")
        out.append("-" * 72)
        out.append(f"  {simulated['count']} mutation(s) non executee(s).")
        out.append("  Les controles de connexion, de schema, de privilege et")
        out.append("  d'espace ont en revanche ete reellement executes.")
        for item in simulated["mutations_withheld"]:
            out.append(f"    - {redact(str(item))}")
        out.append("")

    error = doc.get("error")
    if error:
        out.append("-" * 72)
        out.append("Erreur")
        out.append("-" * 72)
        out.append(f"  {redact(str(error.get('message', '')))}")
        if error.get("step"):
            out.append(f"  etape      : {error['step']}")
        for item in error.get("detail") or []:
            out.append(f"  detail     : {redact(str(item))}")
        if error.get("hint"):
            out.append(f"  remede     : {redact(str(error['hint']))}")
        out.append("")

    out.append("=" * 72)
    return "\n".join(out)


def _check_lines(check: Dict[str, Any], *, verbose: bool) -> List[str]:
    mark = _STATUS_MARK.get(check["status"], check["status"])
    lines = [f"[{mark:^5}] {check['name']}"]
    if check.get("message"):
        lines.append(f"        {redact(str(check['message']))}")
    if verbose:
        for item in check.get("detail") or []:
            lines.append(f"          - {redact(str(item))}")
    elif check["status"] in (FAIL, WARN) and check.get("hint"):
        lines.append(f"        remede : {redact(str(check['hint']))}")
    return lines


def _step_line(step: Dict[str, Any]) -> str:
    mark = {
        "done": "OK   ",
        "skipped": "N/A  ",
        "failed": "ECHEC",
        "running": "EN COURS",
        "pending": "-    ",
    }.get(step["status"], step["status"])
    if step.get("carried_over"):
        # L'etape n'a pas ete executee par ce run : elle vient de
        # l'etat relu sur disque. Afficher son statut de la tentative
        # precedente ferait croire a un travail fait maintenant, et
        # c'est le rapport que l'exploitant lit en premier.
        mark = "REPRISE"
    duration = ""
    if step.get("duration_s") is not None:
        duration = f" ({human_seconds(step['duration_s'])})"
    message = f" — {redact(str(step['message']))}" if step.get("message") else ""
    return f"[{mark}] {step['index']:>2}. {step['name']:<34}{duration}{message}"


def _artifact_lines(artifacts: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for key in sorted(artifacts):
        value = artifacts[key]
        if key == "dump_parts" and isinstance(value, list):
            total = sum(int(p.get("bytes", 0)) for p in value if isinstance(p, dict))
            lines.append(f"dump       : {len(value)} partie(s), {human(total)} au total")
            if len(value) <= 6:
                for part in value:
                    if isinstance(part, dict):
                        lines.append(
                            f"             {part.get('name', '?')}  "
                            f"{human(int(part.get('bytes', 0)))}"
                        )
        elif isinstance(value, (int, float)):
            lines.append(f"{key:<11}: {value}")
        else:
            lines.append(f"{key:<11}: {redact(str(value))}")
    return lines


# --------------------------------------------------------------------------
# Ecriture
# --------------------------------------------------------------------------

def write_reports(
    doc: Dict[str, Any],
    report_dir: Path,
    fmt: str = "text",
    *,
    verbose: bool = False,
) -> List[Path]:
    """Ecrit le rapport dans le repertoire demande et rend les chemins.

    Les fichiers sont en `0600` et le repertoire en `0700` : un rapport
    contient l'inventaire des objets d'un schema, ce qui est une
    information sur le systeme d'information, pas une donnee publique.
    """
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(report_dir, 0o700)
    except OSError:  # pragma: no cover
        pass

    written: List[Path] = []
    run_id = str(doc["run_id"])
    stamp = _iso(time.time()).replace(":", "").replace("-", "")

    # `REPORT_FORMAT` est un enum en majuscules (cf. config.SCHEMA) :
    # la comparaison est faite ici plutot que normalisee en amont, pour
    # que l'appelant puisse passer `text` sanspendre d'un effet de bord.
    fmt = (fmt or "TEXT").upper()

    if fmt in ("TEXT", "BOTH"):
        path = report_dir / f"report-{run_id}-{stamp}.txt"
        _write_private(path, render_text(doc, verbose=verbose) + "\n")
        written.append(path)

    if fmt in ("JSON", "BOTH"):
        path = report_dir / f"report-{run_id}-{stamp}.json"
        payload = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        _write_private(path, payload + "\n")
        written.append(path)

    return written


def _write_private(path: Path, content: str) -> None:
    """Ecrit un fichier en 0600, sans laisser de fichier lisible.

    `os.open` avec le mode explicite est utilise plutot que `write_text`
    suivi d'un `chmod` : entre les deux, le fichier existerait avec les
    permissions issues de l'umask, ce qui suffirait a une fenetre
    d'exposition.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def summary_line(doc: Dict[str, Any]) -> str:
    """Resume d'une ligne, pour un mail ou une notification d'ordonnanceur.

    Volontairement sans detail : c'est la ligne que l'on lit sans ouvrir
    le rapport.
    """
    outcome = doc["outcome"]
    state = "SUCCES" if outcome["success"] else f"ECHEC {outcome['code']}"
    return (
        f"[osd] {state} | {doc['duplication']['source_schema']}"
        f" -> {doc['duplication']['target_schema']}"
        f" | {human_seconds(doc['timing']['duration_s'])}"
        f" | run {doc['run_id']}"
    )


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def _tool_version() -> str:
    from .. import __version__

    return __version__


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def human_seconds(seconds: float) -> str:
    """Formate une duree, sans dependre de la locale."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}j{hours:02d}h"
