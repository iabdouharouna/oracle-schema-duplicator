"""Machine a etats des 19 etapes, etat persistant et reprise.

Le workflow de AGENTS.md est lineaire mais n'est pas atomique : une
duplication reelle peut durer des heures et etre interrompue (cron,
redemarrage, reseau). L'outil doit donc pouvoir repondre a deux
questions a tout moment :

* qu'est-ce qui a deja ete fait, et avec quelle empreinte de dump ?
* peut-on reprendre sans tout refaire, et a partir d'ou ?

L'etat est un fichier JSON en `0600` dans le repertoire de travail, mis a
jour apres chaque etape. Il ne contient aucun secret, mais il contient
des noms d'objets metier : d'ou les permissions restrictives.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import exit_codes as ec
from .errors import OsdError

STATE_VERSION = 1

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

TERMINAL_OK = (DONE, SKIPPED)

#: Etape d'export dans l'ordre canonique du workflow (`AGENTS.md`).
#:
#: Ce numero est repris ici, dans un module qui n'a aucune raison de
#: connaitre le pipeline : `state.py` doit rester utilisable seul, et
#: un test le verifie contre `pipeline.STEP_NAMES` pour que les deux ne
#: puissent pas diverger. Le decalage d'une unite entre l'etape
#: « preparer-datapump » et « exporter » resterait invisible dans les
#: tests unitaires, et ferait declarer reprenable un run dont le dump
#: n'a jamais ete produit.
STEP_EXPORT = 11


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StepState:
    """Etat d'une etape."""

    index: int
    name: str
    status: str = PENDING
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_s: Optional[float] = None
    code: int = ec.SUCCESS
    message: str = ""
    detail: List[str] = field(default_factory=list)

    #: Cette etape vient-elle d'une tentative **anterieure** ?
    #:
    #: Positionne au rechargement du fichier d'etat, efface des que
    #: l'etape est rejouee. `is_done` reste vrai dans les deux cas :
    #: c'est ce qui permet a une reprise de savoir ce qu'elle peut
    #: sauter. Ce que le rapport ne doit pas faire, en revanche, est
    #: presenter comme realise par ce run ce qu'il a seulement relu.
    #:
    #: Sans ce drapeau, une reprise qui echoue a l'etape 14 affichait
    #: les etapes 15 a 19 avec leur statut de la tentative reussie
    #: precedente — y compris « 19. retourner-code : code 0 » sur un
    #: processus qui rendait 6. Le journal, lui, s'arretait bien a 14 :
    #: le rapport et le journal disaient alors deux choses opposees, et
    #: c'est le rapport qui se lit en premier.
    carried_over: bool = False

    @property
    def is_done(self) -> bool:
        return self.status in TERMINAL_OK

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], *, index: int, name: str = ""
    ) -> "StepState":
        """Recharge une etape, en lui fournissant ce que le JSON n'a pas.

        `index` et `name` sont des champs **obligatoires** du
        dataclass, mais rien ne garantit qu'ils soient presents dans un
        fichier d'etat : une version anterieure de l'outil, un
        editeur, ou une ecriture interrompue entre la creation du
        JSON et l'ajout de l'etape. Les exiger ici transformerait un
        fichier incomplet en `TypeError` — c'est-a-dire en « erreur
        interne », sans l'instruction utile de supprimer le fichier et
        de repartir.

        L'index est fourni par `State.from_dict` a partir de la **cle**
        du dictionnaire, jamais de la valeur. La cle fait autorite :
        c'est elle qui indexe `state.steps`, donc une valeur divergente
        deposerait l'etape la ou aucun appelant ne la cherchera. Le nom
        est repris de la valeur quand elle existe, pour qu'une reprise
        n'affiche pas des etapes anonymes alors que l'information est
        disponible.
        """
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        payload["index"] = index
        payload["name"] = str(data.get("name") or name or "")
        return cls(**payload)


@dataclass
class State:
    """Etat complet d'une execution."""

    run_id: str
    version: int = STATE_VERSION
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    source: str = ""
    target: str = ""
    source_schema: str = ""
    target_schema: str = ""
    dry_run: bool = False
    final_code: Optional[int] = None
    steps: Dict[int, StepState] = field(default_factory=dict)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None

    # -- Acces aux etapes -------------------------------------------------
    def step(self, index: int, name: str = "") -> StepState:
        st = self.steps.get(index)
        if st is None:
            st = StepState(index=index, name=name or f"step{index:02d}")
            self.steps[index] = st
        return st

    def start(self, index: int, name: str) -> StepState:
        st = self.step(index, name)
        st.status = RUNNING
        st.started_at = _now()
        st.ended_at = None
        st.duration_s = None
        st.code = ec.SUCCESS
        st.message = ""
        st.detail = []
        # L'etape est rejouee ici : ce qu'elle affirme devient celui de
        # cette invocation, et non plus celui d'une tentative anterieure.
        st.carried_over = False
        return st

    def finish(
        self,
        index: int,
        name: str,
        *,
        status: str = DONE,
        code: int = ec.SUCCESS,
        message: str = "",
        detail: Optional[List[str]] = None,
    ) -> StepState:
        st = self.step(index, name)
        st.status = status
        st.ended_at = _now()
        if st.started_at:
            try:
                start = datetime.fromisoformat(st.started_at)
                end = datetime.fromisoformat(st.ended_at)
                st.duration_s = round((end - start).total_seconds(), 3)
            except ValueError:  # pragma: no cover - horodatage corrompu
                st.duration_s = None
        st.code = code
        st.message = message
        st.detail = list(detail or [])
        return st

    def completed_indices(self) -> List[int]:
        return sorted(i for i, s in self.steps.items() if s.is_done)

    def next_pending(self, ordered: List[int]) -> Optional[int]:
        """Première etape non validee dans l'ordre du workflow."""
        for index in ordered:
            st = self.steps.get(index)
            if st is None or not st.is_done:
                return index
        return None

    def is_resumable(self, ordered: List[int]) -> bool:
        """Indique si un run precedent peut etre repris.

        Deux conditions, et non une : l'etape d'export doit etre
        **terminee**, et les parties du dump doivent etre **recensees**.
        L'une sans l'autre ne suffit pas et les deux manquantes ne se
        rattrapent pas :

        * export termine, aucune partie recensee — l'export n'a produit
          aucun fichier, typiquement sur un schema vide. Reprendre
          importerait un dump inexistant ;
        * parties recensees, export non termine — le fichier existe
          peut-etre, mais le run s'est arrete en cours de generation
          d'autres parties. Importer un jeu incomplet echouerait sur
          ORA-39059, apres avoir annonce que la reprise etait possible.

        L'export est exige au statut `DONE`, et non seulement « termine
        au sens de `TERMINAL_OK` ». Un run limite a un perimetre — un
        `check`, qui s'arrete a l'etape 9 — laisse les etapes suivantes
        au statut `SKIPPED`, donc `is_done` au sens general. Les prendre
        pour un export reussi ferait annoncer une reprise possible sur
        un run qui n'a rien exporte, et qui se terminerait en succes
        sans avoir rien fait.
        """
        export = self.steps.get(STEP_EXPORT)
        if export is None or export.status != DONE:
            return False
        return bool(self.artifacts.get("dump_parts"))

    # -- Serialisation ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "target": self.target,
            "source_schema": self.source_schema,
            "target_schema": self.target_schema,
            "dry_run": self.dry_run,
            "final_code": self.final_code,
            "steps": {str(k): v.to_dict() for k, v in sorted(self.steps.items())},
            "artifacts": self.artifacts,
            "metrics": self.metrics,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "State":
        state = cls(run_id=data.get("run_id", "-"))
        state.version = data.get("version", STATE_VERSION)
        state.created_at = data.get("created_at", _now())
        state.updated_at = data.get("updated_at", state.created_at)
        state.source = data.get("source", "")
        state.target = data.get("target", "")
        state.source_schema = data.get("source_schema", "")
        state.target_schema = data.get("target_schema", "")
        state.dry_run = bool(data.get("dry_run", False))
        state.final_code = data.get("final_code")
        state.artifacts = dict(data.get("artifacts") or {})
        state.metrics = dict(data.get("metrics") or {})
        state.error = data.get("error")
        for key, value in (data.get("steps") or {}).items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                # Une cle non numerique n'identifie aucune etape du
                # workflow. La sauter plutot qu'echouer : le run peut
                # avoir ete interrompu en plein ajourdissement du
                # dictionnaire, et perdre une entree vaut mieux que
                # perdre la reprise entiere.
                continue
            if not isinstance(value, dict):
                continue
            state.steps[index] = StepState.from_dict(
                value, index=index, name=str(value.get("name") or "")
            )
            # Tout ce qui est relu vient d'une tentative anterieure.
            # L'etape 14 d'un run arrete sur une erreur garderait sinon
            # son statut et son message d'avant — « 19. retourner-code :
            # code 0 » sur un processus qui rend 6.
            state.steps[index].carried_over = True
        return state


class StateStore:
    """Charge et sauvegarde l'etat sur disque, de facon atomique.

    L'ecriture atomique (fichier temporaire + `os.replace`) evite qu'une
    coupure au milieu de l'ecriture laisse un fichier d'etat tronque, ce
    qui empecherait toute reprise alors que l'etat precedent etait valide.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Optional[State]:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OsdError(
                f"fichier d'etat corrompu : {self.path} ({exc})",
                ec.CONFIG,
                hint="Supprimer le fichier pour repartir d'une execution propre.",
            ) from None
        except OSError as exc:
            raise OsdError(
                f"lecture de l'etat impossible : {self.path} ({exc.strerror})",
                ec.CONFIG,
            ) from None
        return State.from_dict(data)

    def save(self, state: State) -> None:
        state.updated_at = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        # Le repertoire peut avoir ete cree avec un umask laxiste.
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:  # pragma: no cover
            pass


#: Compteur d'appels a `new_run_id` dans ce processus. Il n'entre dans
#: l'identifiant qu'a partir du **deuxieme** appel, pour que le format
#: reste `osd-<horodatage>-<pid>` — celui que lesSalt et les operateurs
#: connaissent — tout en garantissant l'unicite reelle.
_RUN_SEQ = itertools.count(1)


def new_run_id(prefix: str = "osd") -> str:
    """Genere un identifiant de run lisible et unique.

    Le format combine un horodatage UTC et le PID. L'horodatage rend le
    nom **lisible** — on sait quand le run a eu lieu sans ouvrir le
    fichier — et le PID rend unique deux processus distincts, ce qui est
    le cas ordinaire : le serveur de saut lance un `osd` par
    ordonnancement.

    Un compteur s'ajoute au-dela du premier appel, parce que
    horodatage + PID ne suffisent pas a distinguer deux runs d'un meme
    processus et d'une meme seconde : un wrapper, un test
    d'integration, ou un usage en bibliotheque. Sans lui, le second
    run ecraserait le fichier d'etat et le rapport du premier, et
    `status` ne montrerait que le plus recent — la perte serait
    silencieuse, et l'echec porterait sur la mauvaise duplication.
    """
    seq = next(_RUN_SEQ)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if seq == 1:
        return f"{prefix}-{stamp}-{os.getpid()}"
    return f"{prefix}-{stamp}-{os.getpid()}.{seq}"
