"""Runner de substitution pour le mode dry-run.

Le mode simulation n'est pas « le meme code avec des `if dry_run` » : il
remplace le `Runner` par un `NullRunner`, qui **delegue les lectures et
retient les mutations**. Deux consequences utiles :

* le dry-run reutilise exactement le meme chemin de code que l'execution
  reelle, donc il ne peut pas diverger de ce qu'il simule ;
* l'apercu des mutations tombe naturellement dans le rapport, ce qui
  donne la revue avant production sans code supplementaire.

## Pourquoi « lecture reelle, ecriture retenue »

Un dry-run qui n'execute **rien** ne sert a rien : il ne peut dire ni si
la connexion aboutit, ni si le schema existe, ni s'il manque d'espace. Or
ce sont precisement les echecs que l'on veut attraper avant de lancer un
export de plusieurs heures. Toute la valeur du mode est dans la
validation, et la validation est faite de lectures.

Ce qui est retenu est ce qui **modifie** : lancer `expdp`, lancer
`impdp`, ecrire un fichier de transfert, supprimer un artefact. Ces
operations sont les seules pour lesquelles « ne rien faire » est la
promesse faite a l'utilisateur, et les seules dont l'echec laisserait
des traces a nettoyer.

La distinction est portee par un parametre `mutating` declare a chaque
appel, et non par un `if dry_run` reparti dans le code. La difference
est de fond : le `if` aurait deux chemins, susceptibles de diverger au
fil des modifications, alors que le parametre **declare** ce qui mute. Un
appel qui oublierait le parametre vaut `mutating=False`, c'est-a-dire
lecture, donc delegaee : le defaut est le comportement sur, et l'erreur
d'oubli est indolore.

Les lectures deleguees restent donc reelles sur l'hote, avec les seuls
droits de lecture du compte d'exploitation. Elles n'ecrivent rien, et le
rapport porte leur trace exacte : l'exploitant peut donc distinguer ce
qui a ete **verifie** de ce qui a ete **simule**, ce qui est la seule
chose qui rend un dry-run interpretable.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..runner import Result


class NullRunner:
    """Delegue les lectures, retient les mutations.

    `delegate` est le `Runner` reel que celui-ci remplace. Il n'est pas
    optionnel : sans lui, le dry-run ne pourrait ni repondre a
    `has_binary` ni executer les etapes 4 a 9, soit la moitie du
    workflow, et un rapport qui ne les contient pas ne vaut pas le
    rapport d'un `check`.
    """

    kind = "null"

    def __init__(self, delegate, label: str = "dry-run") -> None:
        self.delegate = delegate
        self.host = label
        self.user = getattr(delegate, "user", "")
        #: Arguments des mutations retenues, dans l'ordre. C'est la
        #: substance du dry-run : ce qui **aurait** ete fait.
        self.calls: List[str] = []
        #: Scripts retenus, integres. Ils partent en piece jointe du
        #: rapport, ce qui permet une revue complete du SQL et des
        #: options Data Pump sans avoir a rejouer le run.
        self.scripts: List[str] = []

    # -- Delegation : l'interface reste celle d'un Runner reel -------------

    @property
    def label(self) -> str:
        return f"simule:{self.delegate.label}"

    @property
    def probe_dir(self) -> str:
        return self.delegate.probe_dir

    def has_binary(self, name: str) -> bool:
        return self.delegate.has_binary(name)

    def allows_mutation(self) -> bool:
        """Faux : c'est tout le principe du dry-run.

        Interroge par les chemins qui contournent `run_script` — le
        transfert par `scp`/`rsync`, execute sur le serveur de saut. Sans
        cette capacite, ces chemins s'executeraient pour de vrai et le
        dry-run deplacerait les donnees qu'il pretait ne pas toucher.
        """
        return False

    def run_script(
        self, script: str, *, timeout: Optional[int] = None, mutating: bool = False
    ) -> Result:
        if not mutating:
            return self.delegate.run_script(script, timeout=timeout)
        self.scripts.append(script)
        self.calls.append(_arguments(script))
        return Result(
            rc=0,
            kv={
                "OSD_RC": "0",
                "OSD_DRYRUN": "1",
                "OSD_SCRIPT_BYTES": str(len(script)),
            },
            rows=[],
            stderr="",
            duration_s=0.0,
            command="(dry-run) mutation retenue, non executee",
        )

    def summary(self) -> List[str]:
        """Mutations retenues, rendues en arguments lisibles.

        Seules les lignes injectees comme arguments sont restituees : le
        corps du script est en piece jointe du rapport, pas dans le
        resume, pour ne pas noyer l'exploitant sous 200 lignes de sh.
        """
        return list(self.calls)


#: Affectation d'un argument pose par `build_script`.
#:
#: Ancre sur une frontiere de ligne : `osd_arg1=` peut egalement se
#: rencontrer dans le corps d'un script, ou il ne designe rien.
_ARG_RE = re.compile(r"(?m)^osd_arg\d+=")

#: Facon dont le shell represente une quote simple a l'interieur d'une
#: chaine single-quote : fermeture, quote echappee, reouverture.
_SHELL_QUOTE = "'\\''"

#: Ce que contient un argument qu'aucune quote ne referme.
#:
#: Une valeur tronquee n'est pas un argument illisible, c'est un
#: argument qu'on ne sait pas lire — et il peut contenir l'empreinte
#: d'un mot de passe. Le restituer tel quel mettrait le secret dans le
#: rapport que la redaction devait proteger : le silence est le seul
#: repli honnete.
_TROUNCHE = "[argument non lisible]"


def _arguments(script: str) -> str:
    """Extrait les arguments d'un script genere par `build_script`.

    Une valeur est une chaine single-quote du shell, et une telle chaine
    peut contenir des retours a la ligne : un DDL en contient, un par
    instruction. Lire argument par **ligne** couperait donc la valeur a
    sa premiere ligne. La fermeture de la quote disparaitrait avec, et
    `_unquote`, qui la reconnait a son dernier caractere, laisserait la
    valeur entiere partir telle quelle — empreinte de mot de passe
    comprise — dans un rapport ou la redaction ne voit plus qu'une quote
    ouverte et ne masque rien. Le decoupage se fait donc sur la quote,
    pas sur la ligne.
    """
    out: List[str] = []
    pos = 0
    while True:
        match = _ARG_RE.search(script, pos)
        if match is None:
            break
        debut = match.end()
        if debut < len(script) and script[debut] == "'":
            fin = _end_of_quote(script, debut)
            if fin is None:
                out.append(_TROUNCHE)
                pos = len(script)
                break
            out.append(_unquote(script[debut:fin]).strip())
            pos = fin
        else:
            fin = script.find("\n", debut)
            fin = len(script) if fin < 0 else fin
            out.append(_unquote(script[debut:fin]).strip())
            pos = fin
    return " ".join(out)


def _end_of_quote(text: str, start: int) -> Optional[int]:
    r"""Index juste apres la quote fermante, ou `None` si elle manque.

    Une quote simple a l'interieur n'est pas une fin de chaine : elle est
    ecrite `'\''` — fermeture, quote echappee, reouverture — et le
    parcours doit la franchir d'un bloc. S'arreter au premier `'`
    couperait la valeur a l'exacte endroit ou commence le secret, ce qui
    est pire que de ne rien restituer.
    """
    index = start + 1
    while index < len(text):
        if text[index] == "'":
            if text.startswith(_SHELL_QUOTE, index):
                index += len(_SHELL_QUOTE)
                continue
            return index + 1
        index += 1
    return None


def _unquote(quoted: str) -> str:
    """Retire les simples quotes posees par le runner."""
    value = quoted.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("'\\''", "'")
    return value
