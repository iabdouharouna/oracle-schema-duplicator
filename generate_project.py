#!/usr/bin/env python3
"""Copie une arborescence propre du projet.

Produit, dans un repertoire cible, exactement les fichiers **versionnes**
du depot : ni artefact de run, ni `__pycache__`, ni configuration
reelle, ni `.git`.

Usage :

    ./generate_project.py /chemin/cible
    ./generate_project.py /chemin/cible --overwrite

## Pourquoi ce script copie au lieu de contenir le projet

La version initiale **embarquait le contenu** de chaque fichier dans un
dictionnaire litteral. C'etait le defaut, et la raison pour laquelle le
script n'a pas suivi la premiere evolution du projet :

* toute modification de `AGENTS.md`, `README.md`, `.gitignore`,
  `config.example.conf` ou d'un document existait desormais en **deux**
  endroits, et la copie ne suivait pas ;
* les stubs `src/*.sh` qu'il generait decrivaient un design Bash
  abandonne, et il recreait a chaque execution des fichiers supprimes ;
* il ne reproduisait ni `bin/osd`, ni `src/osd/`, ni `shell/`, ni les
  tests — c'est-a-dire l'essentiel de l'outil.

« Mettre a jour » ces copies aurait signife recopier 22 000 lignes de
Python dans un dictionnaire : la duplication serait entiere, et
reviendrait au meme point au prochain changement. Un generateur qu'on
rafraichit est un generateur qu'on oublie de rafraichir.

La duplication est donc supprimee : ce script **copie les fichiers
reels**, et n'a plus de contenu a maintenir. `git ls-files` est la seule
source de verite, ce qui rend impossible qu'un artefact s'y glisse.

## Regle unique

Sont copies les fichiers listes par `git ls-files`, mode executable
compris. Le depot **est** un depot git : c'est ce qui rend la regle
suffisante, et il n'y a donc pas de seconde liste a maintenir en
parallele, qui divergerait de la premiere.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

#: Racine du depot : ce script en est a la racine.
RACINE = Path(__file__).resolve().parent

#: Mode git d'un fichier executable, tel que `git ls-files -s` le rend.
_MODE_EXEC = "100755"


def fichiers_versionnes() -> List[Tuple[str, bool]]:
    """Retourne les fichiers versionnes, avec leur bit executable.

    Le mode est relu plutot que devine : `bin/osd` doit rester
    executable chez celui qui recoit la copie, et un `chmod` arbitraire
    le rendrait dependant de l'umask de celui qui l'a copie.
    """
    try:
        sortie = subprocess.run(
            ["git", "-C", str(RACINE), "ls-files", "-s"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        raise SystemExit(
            "git est introuvable : la liste des fichiers versionnes ne "
            "peut pas etre etablie.\n"
            "Ce script ne connait que les fichiers du depot, par "
            "necessite — une liste en dur divergerait de lui."
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"`git ls-files` a echoue ({exc.returncode}) : le repertoire "
            f"{RACINE} n'est pas un depot git.\n"
            f"Erreur : {exc.stderr.strip()}"
        )

    resultats: List[Tuple[str, bool]] = []
    for ligne in sortie.splitlines():
        if not ligne.strip():
            continue
        # `<mode> <sha> <etape>\t<chemin>`
        meta, _, chemin = ligne.partition("\t")
        mode = meta.split(" ", 1)[0]
        if chemin:
            resultats.append((chemin, mode == _MODE_EXEC))
    return resultats


def copier(project_dir: Path, overwrite: bool = False) -> None:
    """Copie les fichiers versionnes dans `project_dir`."""
    project_dir.mkdir(parents=True, exist_ok=True)

    crees: List[str] = []
    ignores: List[str] = []
    for relatif, executable in fichiers_versionnes():
        source = RACINE / relatif
        cible = project_dir / relatif
        cible.parent.mkdir(parents=True, exist_ok=True)

        if cible.exists() and not overwrite:
            ignores.append(relatif)
            print(f"[IGNORER] {relatif}")
            continue

        shutil.copy2(source, cible)
        # `copy2` recopie le mode, mais pas de façon fiable sur tous les
        # systèmes de fichiers ; on le pose explicitement plutôt que de
        # faire confiance au `umask` de la copie.
        if executable:
            cible.chmod(cible.stat().st_mode | 0o111)
        else:
            cible.chmod(cible.stat().st_mode & ~0o111)

        crees.append(relatif)
        print(f"[COPIE]  {relatif}")

    print()
    print("=" * 64)
    print("Copie terminée")
    print("=" * 64)
    print(f"Source       : {RACINE}")
    print(f"Cible        : {project_dir}")
    print(f"Copiés       : {len(crees)}")
    print(f"Ignorés      : {len(ignores)}")
    print()
    print("Le dépôt n'est pas initialisé : le résultat est une copie, "
          "pas un clone.")
    print("Pour repartir de zéro :")
    print(f"  cd {project_dir}")
    print("  git init && git add -A && git commit -m 'import du projet'")


def main() -> None:
    parseur = argparse.ArgumentParser(
        description="Copie une arborescence propre du projet OSD.",
    )
    parseur.add_argument(
        "directory",
        nargs="?",
        default="oracle-schema-duplicator",
        help="Répertoire de destination (défaut : ./oracle-schema-duplicator)",
    )
    parseur.add_argument(
        "--overwrite",
        action="store_true",
        help="Écraser les fichiers existants",
    )
    args = parseur.parse_args()

    project_dir = Path(args.directory).expanduser().resolve()
    if project_dir == RACINE:
        raise SystemExit(
            f"La destination est le dépôt lui-même :\n"
            f"  source = {RACINE}\n"
            f"  cible  = {project_dir}\n"
            "Chaque copie porterait un fichier sur lui-même : aucun gain, "
            "et un journal qui laisse croire à un travail réel.\n"
            "Choisissez un autre répertoire."
        )

    try:
        copier(project_dir, overwrite=args.overwrite)
    except PermissionError as exc:
        print(f"Erreur de permission : {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Erreur système : {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
