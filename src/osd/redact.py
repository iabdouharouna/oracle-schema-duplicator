"""Filtre de masquage des secrets.

Regle absolue du projet : aucun mot de passe ne doit apparaitre dans un
log, un rapport, une commande affichee ou une ligne d'etat. Ce module
centralise le masquage, appele par :

* le formateur de logs (`logging_setup.py`), sur chaque ligne emise ;
* le rendu du rapport texte et JSON ;
* l'affichage des commandes expdp/impdp/sqlplus en mode dry-run ;
* la serialisation de l'etat.

Il ne s'agit pas de rendre le secret « illisible » mais de ne jamais le
laisser sortir du processus qui le detient.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Sequence

MASK = "***REDACTED***"

# Ordre important : le motif "connect string" doit etre traite avant le
# motif generique "cle=valeur", sinon ce dernier ne verrait plus que la
# partie deja masquee.

#: user/password@host:port/service  et  user/password@ //...
#:
#: Le blanc avant `@` est tolere, et c'est deliberé : c'est exactement la
#: forme que le client Oracle produit dans ses propres messages
#: d'authentification —
#:
#:     ORA-01017: user-name/password@connect string
#:     connexion pour l'utilisateur "HR" (MotDePasse) ...
#:
#: Un motif exigeant `@` immediatement apres le mot de passe laisse donc
#: passer le message d'erreur **le plus courant** sur un mauvais mot de
#: passe, c'est-a-dire celui que l'on cherche justement a ne pas
#:journaliser en clair.
_RE_CONNECT_PW = re.compile(
    r"""(?P<user>[A-Za-z0-9._$#\-]+)/(?P<pw>[^@\s'"()]+)\s*@"""
)

#: SQL*Plus / SQL : IDENTIFIED BY <mot de passe> (eventuellement entre quotes)
#:
#: Le mot-cle `VALUES` est traite explicitement parce que c'est la forme
#: qu'emploie le DDL produit par Data Pump :
#:
#:     CREATE USER "HR" IDENTIFIED BY VALUES 'S:B031DD...;T:D80021...'
#:
#: Ce `S:...;T:...` est l'empreinte du mot de passe de l'utilisateur. La
#: laisser en clair dans un journal serait une fuite reelle, exploitable
#: par force brute hors ligne — et elle se presente dans le `sqlfile`
#: produit par `impdp SQLFILE=`, c'est-a-dire dans un artefact que l'outil
#: conserve. Sans ce traitement, l'alternative `IDENTIFIED BY <mot>`
#: masquerait le mot `VALUES` et laisserait l'empreinte visible.
_RE_IDENTIFIED = re.compile(
    r"""(?i)\b(IDENTIFIED\s+BY)(\s+)(?:(VALUES)(\s+))?("[^"]*"|'[^']*'|\S+)""",
)

#: Wallet, parfile, options type password= / pwd= / secret=
_RE_ASSIGN = re.compile(
    r"""(?i)\b(password|passwd|pwd|pw|secret|passphrase)(\s*=\s*)("[^"]*"|'[^']*'|\S+)""",
)

#: Journalisation d'un parametre Oracle : -W "password=foo"
_RE_SQLNET = re.compile(
    r"""(?i)\(([^)]*?\bpassw(?:or)?d\s*=\s*)([^)]*)\)""",
)

_ALL = (_RE_CONNECT_PW, _RE_IDENTIFIED, _RE_ASSIGN, _RE_SQLNET)


def redact(text: Any) -> str:
    """Masque les secrets dans une chaine et retourne le resultat.

    Ne leve jamais d'exception : la redaction est un dernier rempart, elle
    ne doit pas pouvoir transformer une erreur metier en erreur technique.
    Si la chaine n'est pas un `str`, une representation sure est produite.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        # Evite de faire fuiter un secret contenu dans un objet non texte.
        text = "<objet non journalisable: %s>" % type(text).__name__

    for pattern in _ALL:
        if pattern is _RE_CONNECT_PW:
            text = pattern.sub(
                lambda m: "%s/%s@%s" % (m.group("user"), MASK, ""), text
            )
        elif pattern is _RE_IDENTIFIED:
            # Le mot-cle `VALUES`, s'il est present, est conserve : c'est
            # lui qui donne le sens de la ligne. C'est l'empreinte qui part.
            def _mask_ident(m: "re.Match[str]") -> str:
                if m.group(3):
                    return "%s%s%s%s%s" % (m.group(1), m.group(2), m.group(3), m.group(4), MASK)
                return "%s%s%s" % (m.group(1), m.group(2), MASK)

            text = pattern.sub(_mask_ident, text)
        elif pattern is _RE_ASSIGN:
            text = pattern.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
        else:
            text = pattern.sub(lambda m: "(" + m.group(1) + MASK + ")", text)
    return text


def redact_argv(argv: Sequence[str]) -> List[str]:
    """Masque les secrets dans une liste d'arguments.

    Utilise pour journaliser les commandes effectuement executees. La
    commande reelle reste intacte : cette fonction sert uniquement a
    l'affichage.
    """
    return [redact(a) for a in argv]


def redact_lines(lines: Iterable[str]) -> List[str]:
    """Applique `redact` a chaque ligne, en preservant l'ordre."""
    return [redact(line) for line in lines]
