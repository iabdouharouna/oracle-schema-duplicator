"""Interface en ligne de commande.

Le contrat de sortie est le point le plus important de ce fichier :

* **stdout** ne porte que le rapport et les messages d'action. Il est
  donc capturable par un ordonnanceur ou un pipe ;
* **stderr** ne porte que les diagnostics et le journal ;
* le **code de retour** est l'un des dix codes normalises, sans
  exception, y compris pour une erreur interne inattendue.

`main()` ne leve jamais : toute exception est convertie en code. C'est
la seule facon de garantir qu'un `crontab` ne voit jamais un code
imprevu, qui serait interprete a tort comme un succes.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, config, exit_codes as ec
from .errors import ConfigError, Interrupted, OsdError
from .logging_setup import get_logger, set_run_id, set_step, setup as setup_logging
from .redact import redact
from .state import State, StateStore, new_run_id

#: Sous-commandes exposees. Chacune a un comportement et un code de
#: retour distinct ; `check` est le mode de validation seul, `run` le
#: traitement complet.
COMMANDS = ("run", "check", "resume", "status", "clean", "config", "version")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Point d'entree. Retourne toujours un code normalise."""
    started = time.time()
    try:
        args = _parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit as exc:  # argparse a deja imprime son message
        # `--help` et `--version` sortent avec 0 : c'est un succes.
        # Une **erreur d'usage**, en revanche, sort avec 2 — et 2 est
        # aussi, dans ce projet, le code des prerequis non satisfaits.
        # La collision n'etait pas anodine : sous cron, un
        # `osd rn` mal orthographie se faisait lire comme « les
        # privileges manquent », et l'exploitant allait verifier des
        # privileges alors que le probleme etait une lettre.
        # L'erreur d'usage releve de la configuration : c'est donc le
        # code 1 qu'elle rend.
        code = int(exc.code or 0)
        return code if code == 0 else ec.CONFIG

    try:
        return _dispatch(args, started)
    except OsdError as exc:
        _fail(exc.message, exc.detail, exc.hint, exc.code, args)
        return exc.code
    except KeyboardInterrupt:
        _fail("interruption demandee", [], "Relancer avec `osd resume` "
              "pour reprendre a l'etape en cours.", ec.INTERRUPTED, args)
        return ec.INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - filet de securite
        # Une erreur interne ne doit jamais se deguiser en succes, ni
        # disparaitre dans un traceback sous cron. Le traceback complet
        # part dans le journal s'il existe ; sinon il va sur stderr, pour
        # que l'information ne soit pas perdue au moment ou elle est la
        # plus utile.
        logger = get_logger()
        if logger.handlers:
            logger.exception("erreur interne non anticipee")
        else:
            traceback.print_exc()
        _fail(
            f"erreur interne : {type(exc).__name__}: {exc}",
            [],
            "Consulter le journal ou la sortie d'erreur pour le traceback.",
            ec.CONFIG,
            args,
        )
        return ec.CONFIG


def _dispatch(args: argparse.Namespace, started: float) -> int:
    """Aiguille vers la sous-commande demandee."""
    command = args.command

    if command == "version":
        print(f"osd {__version__}")
        return ec.SUCCESS

    if command == "config":
        return _cmd_config(args)

    cfg = _load_config(args)

    if command == "status":
        return _cmd_status(args, cfg)
    if command == "clean":
        return _cmd_clean(args, cfg)
    if command in ("run", "check", "resume"):
        return _cmd_pipeline(args, cfg, command, started)
    raise ConfigError(f"sous-commande inconnue : {command}")


# --------------------------------------------------------------------------
# Chargement de la configuration
# --------------------------------------------------------------------------

def _load_config(args: argparse.Namespace):
    """Charge la configuration en superposant defauts < fichier < env < CLI.

    L'ordre est significatif : un ordonnanceur doit pouvoir corriger un
    fichier partage par variable d'environnement ou par option, sans le
    modifier sur disque.
    """
    # Les surcharges `--set` sont collectionnees par `SetOverride` dans
    # `args.overrides`. Elles sont fusionnees ici avec les options
    # nommees, apres `parse_args` : les deux chemins aboutissent donc au
    # meme mecanisme de superposition, et une cle inconnue est signalee
    # par la validation du schema dans les deux cas.
    overrides: Dict[str, Any] = dict(getattr(args, "overrides", None) or {})

    path = Path(args.config) if args.config else None
    if path is None and not overrides:
        raise ConfigError(
            "aucune configuration fournie",
            hint="Utiliser --config config/config.conf, ou definir les "
                 "variables OSD_* dans l'environnement, ou surcharger "
                 "directement une cle par --set CLE=VALEUR.",
        )
    return config.load(path, overrides=overrides)


# --------------------------------------------------------------------------
# Sous-commandes
# --------------------------------------------------------------------------

def _cmd_config(args: argparse.Namespace) -> int:
    """Affiche le schema de configuration, sans rien lire ni ecrire."""
    if args.key:
        spec = config.SCHEMA.get(args.key.upper())
        if spec is None:
            known = ", ".join(config.schema_keys())
            _fail(f"cle inconnue : {args.key}", [f"cles valides : {known}"],
                  "", ec.CONFIG, args)
            return ec.CONFIG
        print(f"{args.key.upper()}")
        print(f"  type    : {spec.kind}")
        print(f"  defaut  : {spec.default!r}")
        if spec.choices:
            print(f"  valeurs : {', '.join(spec.choices)}")
        if spec.secret:
            print("  secret  : oui (jamais journalise)")
        if spec.doc:
            print(f"  note    : {spec.doc}")
        return ec.SUCCESS

    for key, default, doc in config.describe():
        flag = " (secret)" if config.SCHEMA[key].secret else ""
        print(f"{key:<28} {default!r}{flag}")
        if doc:
            print(f"    {doc}")
    return ec.SUCCESS


def _cmd_pipeline(
    args: argparse.Namespace, cfg, command: str, started: float
) -> int:
    """Execute `run`, `check` ou `resume`."""
    from .stages import Pipeline

    run_id = str(cfg.get("RUN_ID") or args.run_id or new_run_id())
    work = Path(cfg.get("WORK_DIR"))
    store = StateStore(work / f"state-{run_id}.json")

    if command == "resume":
        state = store.load()
        if state is None:
            return _fail(
                f"aucun etat a reprendre pour le run {run_id}",
                [f"fichier attendu : {store.path}"],
                "Lister les runs disponibles par : osd status --all",
                ec.CONFIG,
                args,
            )
    else:
        state = State(run_id=run_id)

    # -- Journalisation, des la creation des repertoires ---------------
    log_dir = Path(cfg.get("LOG_DIR"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
    except OSError as exc:
        return _fail(f"journalisation impossible : {exc.strerror}",
                     [f"repertoire : {log_dir}"], "", ec.PREREQ, args)
    logger = setup_logging(log_dir, str(cfg.get("LOG_LEVEL")), run_id=run_id,
                           to_stderr=not args.quiet)
    set_run_id(run_id)

    for warning in cfg.warnings:
        logger.warning("%s", warning)

    # `--dry-run` n'existe que sur `run`. L'absence est lue ici plutot
    # que sur un Namespace commun a toutes les sous-commandes : `check`
    # n'a pas a heritage d'une option qui n'a aucun sens pour lui, et
    # ajouter un `dry_run=False` a `check` ferait croire qu'il simule.
    dry_run = bool(cfg.get("DRY_RUN")) or bool(getattr(args, "dry_run", False))
    only = None
    if command == "check":
        # `check` s'arrete a la validation des prerequis : etapes 1 a 9.
        only = list(range(1, 10))

    if bool(cfg.get("ALLOW_DESTRUCTIVE")) and not args.allow_destructive:
        # Les deux sont necessaires : l'option seule disparait au premier
        # cron, la configuration seule autoriserait un `run` quotidien a
        # ecraser. L'avertissement rend la situation visible sans bloquer,
        # puisque c'est l'etape 2 qui refuse, avec le bon code.
        logger.warning(
            "ALLOW_DESTRUCTIVE est defini dans la configuration mais "
            "l'option --allow-destructive n'est pas presente : les "
            "operations destructives restent refusees"
        )

    _install_signal_handlers(logger)

    from .lock import acquire

    store.path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = acquire(cfg, description=f"osd {command} {run_id}")
    except OsdError as exc:
        _fail(exc.message, exc.detail, exc.hint, exc.code, args)
        return exc.code

    pipeline = Pipeline(
        cfg=cfg,
        state=state,
        run_id=run_id,
        dry_run=dry_run,
        allow_destructive=args.allow_destructive,
        resume=(command == "resume"),
        # `--force` n'est propre qu'a `resume` : c'est lui qui rejoue les
        # etapes deja validees. Sur `run`, un run neuf n'a rien a rejouer,
        # et sur `check` la notion n'a pas de sens.
        force=bool(getattr(args, "force", False)),
        only=only,
        # `check` valide les prerequis sans rien ecrire : la creation du
        # compte cible, qui est une ecriture, doit le savoir.
        check_only=(command == "check"),
    )

    code = ec.SUCCESS
    try:
        with lock:
            code = pipeline.execute()
            store.save(state)
    except OsdError as exc:
        state.final_code = exc.code
        state.error = exc.as_dict()
        store.save(state)
        _fail(exc.message, exc.detail, exc.hint, exc.code, args)
        return exc.code
    except KeyboardInterrupt:
        state.final_code = ec.INTERRUPTED
        store.save(state)
        _fail("interruption demandee", [],
              "Reprendre par : osd resume --run-id " + run_id, ec.INTERRUPTED, args)
        return ec.INTERRUPTED
    finally:
        set_step("")

    # -- Rapport ---------------------------------------------------------
    doc = _build_report(cfg, state, pipeline, started)
    written = _write_report(cfg, doc, args)

    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    else:
        from .report import render_text, summary_line

        text = render_text(doc, verbose=args.verbose)
        print(text)
        print(summary_line(doc))
        if not args.quiet:
            logger.info("rapports : %s", ", ".join(str(p) for p in written))
    print(f"etat : {store.path}")
    return code


def _build_report(cfg, state: State, pipeline, started: float) -> Dict[str, Any]:
    from .report import build_document

    return build_document(
        cfg=cfg,
        state=state,
        checks=pipeline.checks,
        started_at=started,
        finished_at=time.time(),
        error=state.error,
        simulated=pipeline.withheld_mutations(),
    )


def _write_report(cfg, doc: Dict[str, Any], args: argparse.Namespace) -> List[Path]:
    from .report import write_reports

    try:
        return write_reports(
            doc,
            Path(cfg.get("REPORT_DIR")),
            str(cfg.get("REPORT_FORMAT")),
            verbose=args.verbose,
        )
    except OSError as exc:
        # Un rapport non ecrit n'invalide pas le run : la duplication est
        # deja faite dans la base. Le code de retour reste donc celui de
        # l'operation. L'incident est signale, parce qu'un exploitant qui
        # ne recoit pas de rapport ne saura pas que la duplication a
        # reussi.
        get_logger().warning("rapport non ecrit : %s", exc)
        return []


def _cmd_status(args: argparse.Namespace, cfg) -> int:
    """Affiche l'etat d'un run, ou la liste des runs connus."""
    work = Path(cfg.get("WORK_DIR"))
    if not work.is_dir():
        print(f"aucun run : {work} est absent ou vide")
        return ec.SUCCESS

    if args.run_id:
        store = StateStore(work / f"state-{args.run_id}.json")
        state = store.load()
        if state is None:
            return _fail(f"run inconnu : {args.run_id}", [f"attendu : {store.path}"],
                         "", ec.CONFIG, args)
        return _print_status(state, verbose=args.verbose)

    files = sorted(work.glob("state-*.json"))
    if not files:
        print(f"aucun run enregistre dans {work}")
        return ec.SUCCESS

    if not args.all and len(files) > 1:
        files = files[-1:]

    print(f"{'run':<34} {'code':<5} {'etat':<9} source -> cible")
    print("-" * 92)
    for path in files:
        state = _load_state(path)
        if state is None:
            continue
        code = state.final_code if state.final_code is not None else "..."
        verdict = "succes" if code == 0 else ec.label(int(code)) if isinstance(code, int) else "en cours"
        print(f"{state.run_id:<34} {str(code):<5} {verdict:<9} "
              f"{state.source_schema} -> {state.target_schema}")
    return ec.SUCCESS


def _cmd_clean(args: argparse.Namespace, cfg) -> int:
    """Supprime les etats et journaux de runs termines.

    La suppression d'etats est un cas particulier de destructivite : un
    etat supprime est une reprise impossible. L'option est donc exigee,
    sauf pour les runs deja termines en echec, dont l'etat n'a plus
    d'usage operationnel — ce qui reste discutable, d'ou le refus par
    defaut.
    """
    work = Path(cfg.get("WORK_DIR"))
    log_dir = Path(cfg.get("LOG_DIR"))
    report_dir = Path(cfg.get("REPORT_DIR"))

    targets: List[Path] = []
    for directory in (work, log_dir, report_dir):
        if not directory.is_dir():
            continue
        for pattern in ("state-*.json", "run-*.log", "report-*.json", "report-*.txt"):
            targets.extend(directory.glob(pattern))

    if not targets:
        print("rien a nettoyer")
        return ec.SUCCESS

    if not args.allow_destructive:
        print(f"{len(targets)} artefact(s) seraient supprime(s) :", file=sys.stderr)
        for path in targets[:20]:
            print(f"  {path}", file=sys.stderr)
        if len(targets) > 20:
            print(f"  ... et {len(targets) - 20} autre(s)", file=sys.stderr)
        return _fail(
            "nettoyage refuse sans autorisation explicite",
            [],
            "Reexecuter avec --allow-destructive. Le nettoyage supprime "
            "l'etat de reprise : l'apres verification, un run repris "
            "repart de l'etape 11.",
            ec.SECURITY,
            args,
        )

    removed = 0
    for path in targets:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            print(f"  suppression impossible : {path} ({exc.strerror})", file=sys.stderr)
    print(f"{removed} artefact(s) supprime(s)")
    return ec.SUCCESS


# --------------------------------------------------------------------------
# Signaux
# --------------------------------------------------------------------------

def _install_signal_handlers(logger) -> None:
    """Convertit SIGINT et SIGTERM en interruption cooperative.

    Le traitement n'est pas un `sys.exit` immediat : il leve une
    exception, ce qui permet aux gestionnaires de contexte de liberer le
    verrou et d'enregistrer l'etat. Un `exit` brutal laisserait un
    verrou bloque et une reprise impossible — les deux choses que le
    mecanisme de reprise existe justement pour eviter.

    Le second signal force la sortie : un exploitant qui a attendu doit
    pouvoir arreter definitivement.
    """
    state = {"count": 0}

    def handler(signum, frame):  # type: ignore[no-untyped-def]
        state["count"] += 1
        if state["count"] == 1:
            logger.warning(
                "signal %s recu : arret propre en cours, "
                "l'etat sera enregistre",
                signal.Signals(signum).name,
            )
            raise Interrupted(f"signal {signal.Signals(signum).name} recu")
        logger.error("signal %s recu a nouveau : arret immediat",
                     signal.Signals(signum).name)
        os._exit(ec.INTERRUPTED)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # pragma: no cover - hors thread principal
            pass


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------

def _print_status(state: State, *, verbose: bool) -> int:
    from .report import human_seconds

    print(f"run       : {state.run_id}")
    code = state.final_code
    print(f"code      : {code if code is not None else 'en cours'} "
          f"({ec.label(code) if isinstance(code, int) else '-'})")
    print(f"duplication: {state.source_schema} -> {state.target_schema}")
    print(f"simulation : {'oui' if state.dry_run else 'non'}")
    if state.created_at:
        print(f"cree le   : {state.created_at}")
    print()
    print(f"{'':2} {'etape':<34} {'statut':<9} {'duree':>9}  message")
    print("-" * 100)
    for _, step in sorted(state.steps.items()):
        duration = human_seconds(step.duration_s) if step.duration_s else ""
        print(f"{step.index:2} {step.name:<34} {step.status:<9} {duration:>9}  "
              f"{redact(step.message)}")
        if verbose and step.detail:
            for item in step.detail:
                print(f"     {'':->6} {'':->11} {'':->9} {'':->9}  {redact(str(item))}")
    if state.error:
        print()
        print(f"erreur : {redact(str(state.error.get('message', '')))}")
        if state.error.get("hint"):
            print(f"remede : {redact(str(state.error['hint']))}")
    return ec.SUCCESS if code in (None, ec.SUCCESS) else int(code)


def _load_state(path: Path) -> Optional[State]:
    try:
        return StateStore(path).load()
    except OsdError:
        return None


def _fail(
    message: str,
    detail: Sequence[str],
    hint: str,
    code: int,
    args: argparse.Namespace,
) -> int:
    """Affiche un diagnostic sur stderr, le journalise, et rend le code.

    Le detail et le remede vont sur stderr, jamais sur stdout : un
    `osd run ... > sortie.txt` doit produire un rapport, pas une trace
    d'erreur melangee a la sortie metier.

    **La fonction rend `code`** et ne l'ignore pas. C'est ce qui permet
    d'ecrire `return _fail(...)` sans risque d'oublier le `return` : un
    `return _fail(...)` qui rend `None` ferait sortir le processus avec 0
    alors qu'une erreur vient d'etre signalee, ce qui est la pire panne
    possible pour un outil commande par cron.
    """
    text = redact(message)
    print(f"osd: {text}", file=sys.stderr)
    for item in list(detail)[:20]:
        print(f"    {redact(str(item))}", file=sys.stderr)
    if hint:
        print(f"  remede : {redact(hint)}", file=sys.stderr)
    logger = get_logger()
    # Le message n'est journalise que si un journal existe deja. Sinon,
    # le `logging` de la bibliotheque standard ecrirait sur stderr avec
    # son propre format, en doublant le diagnostic affiche ci-dessus.
    if logger.handlers:
        logger.error("%s", text)
        if hint:
            logger.error("remede : %s", redact(hint))
    return code


# --------------------------------------------------------------------------
# Analyse des arguments
# --------------------------------------------------------------------------

def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = _build_parser()
    return parser.parse_args(list(argv))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osd",
        description=(
            "Duplication d'un schema Oracle 19c d'une base source vers une "
            "base cible, par Data Pump."
        ),
        epilog=(
            "Codes de retour : 0 succes, 1 configuration, 2 prerequis, "
            "3 connexion, 4 export, 5 transfert, 6 import, 7 validation, "
            "8 securite, 9 interruption."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"osd {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMANDE")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("-c", "--config", metavar="FICHIER",
                         help="fichier de configuration (jamais execute, seulement lu)")
        sub.add_argument("--run-id", metavar="ID", help="identifiant de run")
        sub.add_argument("-v", "--verbose", action="store_true",
                         help="detail complet dans le rapport")
        sub.add_argument("--json", action="store_true",
                         help="rapport JSON sur stdout au lieu du texte")
        sub.add_argument("-q", "--quiet", action="store_true",
                         help="pas de journalisation sur stderr")
        sub.add_argument("--allow-destructive", action="store_true",
                         help=("autorise explicitement une operation "
                               "destructive : ecrasement d'objets, "
                               "suppression d'etats, nettoyage"))
        sub.add_argument("--set", action=SetOverride, dest="overrides", default={},
                         metavar="CLE=VALEUR",
                         help="surcharge une cle de configuration (repetable)")

    p_run = subparsers.add_parser(
        "run", help="execute la duplication complete")
    add_common(p_run)
    p_run.add_argument("--dry-run", action="store_true",
                       help="simule l'integralite du traitement sans ecrire")

    p_check = subparsers.add_parser(
        "check", help="valide la configuration et les prerequis (etapes 1 a 9)")
    add_common(p_check)

    p_resume = subparsers.add_parser(
        "resume", help="reprend un run interrompu")
    add_common(p_resume)
    p_resume.add_argument("--force", action="store_true",
                          help="rejoue les etapes deja validees")

    p_status = subparsers.add_parser("status", help="affiche l'etat d'un run")
    add_common(p_status)
    p_status.add_argument("--all", action="store_true", help="tous les runs connus")

    p_clean = subparsers.add_parser(
        "clean", help="supprime etats et journaux (destructif)")
    add_common(p_clean)

    p_config = subparsers.add_parser(
        "config", help="affiche le schema de configuration")
    p_config.add_argument("key", nargs="?", help="detaille une cle")

    subparsers.add_parser("version", help="affiche la version")

    return parser


class SetOverride(argparse.Action):
    """Action `--set CLE=VALEUR`.

    Le controle de forme est fait ici, et non apres l'analyse : une
    option malformee doit produire un message d'usage, pas une erreur de
    configuration. La cle est de plus normalisee en majuscules, ce qui
    rend `--set parallel=4` et `--set PARALLEL=4` equivalents — une
    commodite, mais une commodite qui evite un echec stupide sous
    contrainte de temps.
    """

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        item = str(values)
        if "=" not in item:
            parser.error(f"{option_string} mal forme : {item!r} "
                         "(attendu : CLE=VALEUR)")
        key, _, value = item.partition("=")
        key = key.strip().upper()
        if not key or not key.replace("_", "").isalnum():
            parser.error(f"{option_string} cle invalide : {key!r}")
        # La valeur est degarnee de ses espaces de bord, et **seulement**
        # ceux-la : les espaces internes restent intacts, parce qu'ils
        # sont significatifs (un chemin de repertoire, un nom de
        # fichier). `--set PARALLEL = 4` doit fonctionner comme
        # `--set PARALLEL=4` : le `=` n'etant pas un separateur du
        # shell, rien d'autre ne peut dedouaner, et une valeur espaces
        # irait casser silencieusement la conversion de type.
        # L'oppose -- ne pas degarresser -- ferait dependre le resultat
        # de la maniere dont la ligne a ete quotee.
        #
        # La derniere occurrence gagne : c'est le comportement attendu
        # quand on repete une option, et l'ordre reste deterministe.
        getattr(namespace, self.dest)[key] = value.strip()


def cli() -> None:  # pragma: no cover - point d'entree console
    """Enveloppe pour le script `bin/osd`."""
    sys.exit(main())
