"""Execution de commandes en local et sur les hotes AIX distants.

Le serveur de saut n'a pas besoin du client Oracle : toute operation
Oracle est executee sur l'hote qui heberge la base, via SSH. Le script
distant est construit ici puis envoye sur stdin :

    ssh -o BatchMode=yes <hote> sh -s   <  script_complet

Les arguments ne transitent **pas** par la ligne de commande distante.
Ils sont injectes dans le script sous forme de variables entre simples
quotes, ce qui evite simultanement le quoting d'OpenSSH (qui concatene
les arguments sans les proteger) et toute possibilite d'injection.

Trois consequences de ce choix, a garder en tete :

* le script complet est journalisable et rejouable a l'identique ;
* `ps` sur l'hote ne montre que `sh -s` : ni le SQL, ni un eventuel
  mot de passe de parfile ne sont visibles dans la table des
  processus ;
* aucun `eval` n'est employe, conformement a AGENTS.md.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import exit_codes as ec
from .errors import OsdError, PrereqError
from .redact import redact, redact_argv

SHELL_DIR = Path(__file__).resolve().parent.parent.parent / "shell"

#: Delai maximal d'une commande distante. Un `expdp` peut durer des
#: heures, donc le delai n'est applique qu'aux commandes de diagnostic ;
#: les appels metier passent `timeout=None`.
DEFAULT_TIMEOUT = 120

#: Codes ORA-XXXXX. La liste est volontairement large et volontairement
#: incomplete : elle sert a la classification, pas de reference normative.
_ORA_RE = re.compile(r"ORA-\d{5}")

#: Nom de variable d'environnement accepte dans `build_script(env=...)`.
#: Strict, parce que le nom est place dans le script **sans quoting** : il
#: doit pouvoir etre un nom de variable shell, rien d'autre.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Variables dont l'ecrasement casserait le contrat d'execution. Le
#: prelude et le corps s'appuient sur elles ; les modifier produirait un
#: echec obscur (commande introuvable, `set -e` desactive, sortie
#: redirigee) bien plus tard, sur un hote de production, pour une cause
#: invisible dans la trace. Elles sont refusees plutot que tolerees.
_ENV_DENIED = frozenset({
    "PATH",     # prelude et corps appellent wc, tr, rm, command...
    "IFS",     # change la separation des mots du shell
    "PS4",     # trace xtrace
    "SHELL",   # interpreteur des sous-shells
    "BASH_ENV", "ENV",  # fichiers de demarrage sous sh
    "LD_PRELOAD", "LD_LIBRARY_PATH",  # injection de bibliotheque
})


@dataclass
class Result:
    """Resultat structure d'une execution distante."""

    rc: int
    kv: Dict[str, str] = field(default_factory=dict)
    rows: List[str] = field(default_factory=list)
    stderr: str = ""
    stdout_raw: str = ""
    duration_s: float = 0.0
    command: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def get(self, key: str, default: str = "") -> str:
        return self.kv.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.kv.get(key, default))
        except (TypeError, ValueError):
            return default

    def as_safe_dict(self) -> Dict[str, Any]:
        return {
            "rc": self.rc,
            "kv": {k: redact(v) for k, v in self.kv.items()},
            "rows": len(self.rows),
            "duration_s": round(self.duration_s, 3),
        }


class RemoteProtocolError(OsdError):
    """Le bloc de resultat distant est absent ou illisible.

    Cela signifie soit que le script n'a pas ete transmis correctement,
    soit que la commande distante a echoue avant le trap. Les deux cas
    doivent etre distinguables par l'exploitant : le stderr distant est
    integralement recopie dans le message.
    """


def _single_quote(value: str) -> str:
    """Cite une chaine pour un shell POSIX.

    La seule facon sure de placer une chaine litterale en shell est
    l'echappement du guillemet simple par une quote vide. Un retour a la
    ligne reste litteral, ce qui est un bonus : le SQL multi-lignes
    fonctionne sans traitement particulier.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def load_body(name: str) -> str:
    """Charge un corps de script distant depuis `shell/`.

    Le fichier est lu a chaque appel plutot que mis en cache : il peut
    etre corrige entre deux executions, et cela evite un cache invalide
    difficile a diagnostiquer.
    """
    path = SHELL_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PrereqError(f"script distant introuvable : {path}") from None


class Raw(str):
    """Marqueur : la valeur est inseree telle quelle dans le script.

    Reserve a l'expansion de variables **deja definies** : par le
    prelude, ou par le `bootstrap`, qui est insere avant les arguments
    (par exemple `Raw('"$osd_parpath"')`).

    L'antecedent est indispensable sous `set -u` : designer une variable
    absente arrete le script avant meme qu'il ait fait quoi que ce soit.
    A n'utiliser qu'avec une chaine construite par le code, jamais avec
    une valeur provenant de la configuration ou d'une sortie d'outil :
    c'est le seul endroit ou du texte non quote atteint le script
    distant.
    """


def build_script(
    body: str,
    argv: Sequence[str],
    *,
    bootstrap: str = "",
    env: Optional[Dict[str, str]] = None,
) -> str:
    """Assemble le script complet, dans cet ordre :

        environnement -> prelude -> amorcage -> arguments -> corps

    L'ordre n'est pas indifferent, et les deux inversions possibles sont
    des erreurs reellement observees :

    * `env` avant le prelude, parce que le client Oracle est lance par le
      corps et qu'une valeur posee apres le prelude n'aurait d'effet sur
      rien ;
    * `bootstrap` avant les arguments, parce qu'un `Raw` ne peut designer
      qu'une variable **deja posee**. C'est ainsi que le parfile Data
      Pump est ecrit par l'amorcage puis designe par l'argument
      `Raw('"$osd_parpath"')`. Inverse, le script evaluait sous `set -u`
      `osd_parpath: unbound variable` et mourait avant l'export.

    `bootstrap` ne peut donc pas referencer `osd_argN`. Aucun appelant n'a
    besoin de cela : ce qu'un amorcage materialise — un parfile — lui est
    fourni par l'appelant, qui connait deja les valeurs.

    Les variables `osd_argN` sont recombinees par `set --` pour que le
    corps utilise `"$@"` comme un script normal.

    Les valeurs d'environnement sont posees par `export` avec un nom
    **valide en shell**, ce qui est verifie ici plutot que suppose : un
    nom forge par une configuration tierce ne doit pas pouvoir introduire
    une commande.
    """
    if not argv:
        raise ValueError("argv vide")

    parts: List[str] = []
    if env:
        parts.append("# --- environnement requis par le client Oracle ---")
        for name, value in sorted(env.items()):
            if not _ENV_NAME_RE.match(name):
                raise ValueError(f"nom de variable d'environnement invalide : {name!r}")
            if name in _ENV_DENIED:
                raise ValueError(
                    f"variable d'environnement protegee, non transmissible : {name}"
                )
            if value:
                parts.append(f"{name}={_single_quote(str(value))}")
                parts.append(f"export {name}")
        parts.append("")
    parts.append(load_body("prelude.sh"))
    parts.append("")
    if bootstrap:
        parts.append("# --- amorcage ---")
        parts.append(bootstrap)
        parts.append("")
    assignments: List[str] = []
    refs: List[str] = []
    for index, value in enumerate(argv, start=1):
        if isinstance(value, Raw):
            assignments.append(f"osd_arg{index}={value}")
        else:
            assignments.append(f"osd_arg{index}={_single_quote(str(value))}")
        refs.append(f'"$osd_arg{index}"')
    parts.append("# --- arguments generes par le serveur de saut ---")
    parts.extend(assignments)
    parts.append("set -- " + " ".join(refs))
    parts.append("")
    parts.append("# --- corps du script ---")
    parts.append(body)
    return "\n".join(parts)


class LocalRunner:
    """Execute le meme protocole, sans SSH, sur la machine courante.

    Utilise quand l'outil est installe directement sur un serveur de base
    Linux, ou dans les tests d'integration : le code de chemin est alors
    rigoureusement identique a celui du cas distant.
    """

    kind = "local"

    def __init__(self, host: str = "", user: str = "", ssh_opts: Sequence[str] = ()) -> None:
        self.host = host or "localhost"
        self.user = user
        self.ssh_opts = list(ssh_opts)
        self.probe_dir = os.environ.get("TMPDIR", "/tmp")

    def has_binary(self, name: str) -> bool:
        from shutil import which

        return which(name) is not None

    def allows_mutation(self) -> bool:
        """Ce runner autorise-t-il une operation qui modifie l'existant ?

        Vrai pour les runners reels. Le `NullRunner` du dry-run renvoie
        faux. Cette capacite est interrogee par les chemins qui ne
        passent pas par `run_script` — le transfert, execute par `scp`
        ou `rsync` sur le serveur de saut — afin qu'ils soient retenus
        eux aussi. Sans elle, un dry-run partagerait son chemin de code
        avec le run reel mais pas ses contraintes, et le rapport
        afficherait un transfert « simule » alors que les octets
        seraient deplaces.
        """
        return True

    @property
    def label(self) -> str:
        return f"local:{self.host}"

    def run_script(
        self,
        script: str,
        *,
        timeout: Optional[int] = DEFAULT_TIMEOUT,
        mutating: bool = False,
    ) -> Result:
        """Execute `script` par `sh -s`, sans shell intermediaire.

        `mutating` n'a aucun effet ici : il n'existe que pour que le
        `Runner` reel et le `NullRunner` du dry-run partagent une
        signature. Ignorer le parametre est deliberement plus sur que de
        refuser l'appel : un appelant qui l'annexe par erreur dans du
        code reel ne doit pas voir son export bloque par un `TypeError`.
        """
        argv = ["/bin/sh", "-s"]
        try:
            proc = subprocess.run(
                argv,
                input=script.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=_clean_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise PrereqError(
                f"delai depasse ({timeout}s) sur {self.label}",
                detail=[redact((exc.stderr or b"").decode("utf-8", "replace"))],
            ) from None
        except OSError as exc:
            raise PrereqError(f"/bin/sh indisponible : {exc.strerror}") from None
        return _parse_result(
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
            proc.returncode,
            command=f"/bin/sh -s (script {len(script)} octets)",
        )


class RemoteRunner:
    """Execute un script distant via `ssh ... sh -s`."""

    kind = "remote"

    def __init__(
        self,
        host: str,
        user: str = "",
        ssh_opts: Sequence[str] = (),
        *,
        identity: str = "",
        control_path: str = "",
        probe_dir: str = "",
    ) -> None:
        if not host:
            raise PrereqError("hote SSH non configure pour l'execution distante")
        self.host = host
        self.user = user
        self.ssh_opts = list(ssh_opts)
        self.identity = identity
        self.control_path = control_path
        # Repertoire de travail utilise pour les temoins de la sonde de
        # transfert. Par defaut `/tmp` : il est presque toujours
        # accessible a l'utilisateur qui lance `expdp`, et il n'exige
        # aucune privilege. Un chemin lying (bien reel) n'est pas connu
        # du runner, qui n'a pas vocation a parler Oracle.
        self.probe_dir = probe_dir or "/tmp"
        self._binaries: Dict[str, bool] = {}

    @property
    def label(self) -> str:
        return f"ssh:{self.user}@{self.host}" if self.user else f"ssh:{self.host}"

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def allows_mutation(self) -> bool:
        """Toujours vrai : voir `LocalRunner.allows_mutation`."""
        return True

    def has_binary(self, name: str) -> bool:
        """Indique si un binaire est dans le PATH de l'hote.

        Le resultat est memorise : le workflow pose la question au plus
        deux fois par hote, et chaque appel coute un aller-retour SSH.
        """
        if name in self._binaries:
            return self._binaries[name]
        found = False
        try:
            result = self.run_script(
                build_script(load_body("remote_which.sh"), [name]), timeout=60
            )
            found = result.rc == 0 and result.get("OSD_FOUND") == "1"
        except OsdError:
            found = False
        self._binaries[name] = found
        return found

    def argv(self) -> List[str]:
        """Construit la ligne de commande `ssh` effective, pour les logs.

        Aucun secret n'y figure : l'authentification repose sur cle, et
        `BatchMode=yes` interdit toute invite de mot de passe — ce qui est
        indispensable sous cron, ou une invite bloquerait indefiniment.
        """
        # `BatchMode=yes` est **impose**, pas repris tel quel. La
        # validation de configuration refuse deja toute autre valeur, mais
        # cette classe est construite directement par les tests et par
        # tout appelant futur : elle ne peut pas faire confiance a une
        # validation amont qu'elle ne voit pas passer. Et l'enjeu est trop
        # cher — un run bloque jusqu'a l'expiration du crontab, sans
        # journal, sans code de sortie, et un `crontab` que l'exploitant
        # ne remarque pas.
        argv = ["ssh", "-o", "BatchMode=yes"]
        for opt in self.ssh_opts:
            if opt.strip() and not opt.strip().lower().startswith("batchmode"):
                argv.extend(["-o", opt])
        if self.identity:
            argv.extend(["-i", self.identity])
        if self.control_path:
            # multiplexing : evite une nouvelle authentification a chaque
            # appel, ce qui compte quand le workflow en fait des dizaines.
            argv.extend(["-o", f"ControlPath={self.control_path}"])
            argv.extend(["-o", "ControlMaster=auto"])
            argv.extend(["-o", "ControlPersist=300"])
        argv.append(self.target)
        # `sh -s` : lecture du script sur stdin, arguments vides. Aucune
        # donnee sensible dans la ligne de commande distante.
        argv.extend(["sh", "-s"])
        return argv

    def run_script(
        self,
        script: str,
        *,
        timeout: Optional[int] = DEFAULT_TIMEOUT,
        mutating: bool = False,
    ) -> Result:
        """Envoie `script` sur l'entree standard de `ssh ... sh -s`.

        `mutating` est ignore, comme dans `LocalRunner` : seule la
        signature est partagee. Le mode dry-run est traite en amont, par
        substitution du runner, et non par un test sur ce parametre —
        ce qui maintiendrait l'invariant « un seul point de decision ».
        """
        argv = self.argv()
        try:
            proc = subprocess.run(
                argv,
                input=script.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=_clean_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise PrereqError(
                f"delai depasse ({timeout}s) sur {self.label}",
                detail=[redact((exc.stderr or b"").decode("utf-8", "replace"))],
            ) from None
        except FileNotFoundError:
            raise PrereqError(
                "client ssh absent du serveur de saut",
                hint="Installer openssh-client.",
            ) from None
        except OSError as exc:
            raise PrereqError(f"echec SSH vers {self.label} : {exc.strerror}") from None

        return _parse_result(
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
            proc.returncode,
            command=" ".join(redact_argv(argv)),
        )


def _clean_env() -> Dict[str, str]:
    """Environnement minimal et sans surprise pour l'execution distante.

    `LC_ALL` est forcee a `C` : le contracte du bloc de resultat impose
    un format stable, et la sortie de `df` comme le comportement de
    `sort` dependent de la locale. Cote SQL*Plus, c'est la locale du
    *client* qui compte, et elle est reglee par la commande elle-meme.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": os.environ.get("TZ", "UTC"),
        "HOME": os.environ.get("HOME", "/"),
        "TERM": "dumb",
    }
    # Conserve explicitement : sans cela, un `expdp` distant herite d'un
    # ORACLE_HOME incoherent avec le client reellement installe.
    for key in ("ORACLE_HOME", "ORACLE_SID", "TNS_ADMIN", "NLS_LANG"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


#: Marqueur de fin du bloc machine. La reconnaissance est **ancree en
#: tete de ligne** : c'est la seule garantie qui compte, car elle
#: empeche qu'une valeur de donnee contenant `OSD_RESULT_END` au milieu
#: d'un texte soit prise pour un marqueur, donc qu'un bloc soit tronque
#: par sa propre charge utile.
#:
#: L'ancrage est rendu possible par le saut de ligne inconditionnel que
#: le prelude emet avant le marqueur (cf. `osd_finish`). Sans lui, un
#: corps dont la sortie ne se termine pas par un saut de ligne collerait
#: sa donnee au marqueur, et le bloc serait rejete comme incomplet.
#:
#: Le `rc=` n'est pas exige : le prelude l'emet toujours, mais un shell
#: distant qui evoluerait degraderait alors vers le code de retour du
#: processus plutot que vers une exception qui masquerait la sortie.
_END_MARKER = "OSD_RESULT_END"


def _is_end_marker(line: str) -> bool:
    return line.startswith(_END_MARKER)


def _parse_result(stdout: str, stderr: str, returncode: int, *, command: str) -> Result:
    """Analyse le bloc de resultat produit par le script distant.

    La sortie de travail du script a ete redirigee vers stderr, si bien
    que stdout ne contient, en theorie, que le bloc machine. On reste
    tolerant : des lignes hors structure sont conservees dans
    `stdout_raw` pour le diagnostic, sans faire echouer l'analyse.
    """
    import time  # importe ici pour garder l'en-tete leger

    started = time.monotonic()
    kv: Dict[str, str] = {}
    rows: List[str] = []
    seen_begin = False
    seen_end = False
    in_rows = False

    for line in stdout.splitlines():
        if line == "OSD_RESULT_BEGIN":
            seen_begin = True
            continue
        if line == "OSD_ROWS_BEGIN":
            in_rows = True
            continue
        if line == "OSD_ROWS_END":
            in_rows = False
            continue
        if _is_end_marker(line):
            seen_end = True
            tail = line.partition("rc=")[2].strip()
            if tail.isdigit():
                returncode = int(tail)
            continue
        if in_rows:
            rows.append(line)
            continue
        # `OSD_FATAL` est produit par osd_die : c'est un diagnostic, pas
        # une donnee exploitable, il est donc traite a part. Toute autre
        # ligne `CLE=VALEUR` est une donnee du bloc, y compris celles
        # prefixees par `OSD_` (OSD_RC, OSD_AVAIL_BYTES, ...).
        if line.startswith("OSD_FATAL="):
            kv["__fatal__"] = line.partition("=")[2]
            continue
        if line.startswith("OSD_KV_ERROR="):
            kv["__kv_error__"] = line.partition("=")[2]
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            kv[key] = value

    if not seen_begin or not seen_end:
        raise RemoteProtocolError(
            "bloc de resultat distant incomplet",
            ec.PREREQ,
            detail=[
                f"stdout: {redact(stdout[:2000])}",
                f"stderr: {redact(stderr[:2000])}",
            ],
            hint="Verifier que l'hote execute bien `sh -s` et que le script "
                 "n'a pas ete interrompu. Cause frequente : shell distant "
                 "absent, ousession SSH fermee par BatchMode.",
        )

    return Result(
        rc=returncode,
        kv=kv,
        rows=rows,
        stderr=stderr,
        stdout_raw=stdout,
        duration_s=time.monotonic() - started,
        command=command,
    )


def oracle_error_codes(result: Result) -> List[str]:
    """Extrait les codes ORA-XXXXX presents, quelle que soit la langue."""
    found = set(_ORA_RE.findall(result.stdout_raw))
    found.update(_ORA_RE.findall(result.stderr))
    if result.get("OSD_ORACLE_ERROR") == "1":
        for code in result.get("OSD_ORACLE_CODES").split():
            found.add(code)
    return sorted(found)


def quote_for_log(argv: Sequence[str]) -> str:
    """Rend une commande lisible et sans secret, pour les journaux."""
    return " ".join(shlex.quote(redact(a)) for a in argv)
