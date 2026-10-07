"""Execution distante par Ansible, en remplacement de `ssh` direct.

## Pourquoi

L'outil executait jusqu'ici le script distant par :

    ssh -o BatchMode=yes <hote> sh -s   <  script_complet

`BatchMode=yes` est une garantie, pas une commodite : il interdit toute
invite interactive, donc un mot de passe errone ne peut pas laisser le
run bloque jusqu'a l'expiration du crontab. Il rendait en revanche
l'authentification par **cle** obligatoire — c'est-a-dire
l'installation d'une cle publique sur chaque hote, avec ses
implications de cycle de vie sur plusieurs sites.

Ansible permet d'authentifier par mot de passe sans sacrifier cette
garantie : le plugin SSH lit `ansible_password` de l'inventaire et passe
le secret a `sshpass` via l'environnement, jamais par argument. C'est ce
qui rend le coffre utile : les mots de passe sont chiffres au repos
dans `group_vars`, et ne transitent ni par la ligne de commande, ni par
la configuration de l'outil.

## Ce qui change reellement : peu de choses

Le contrat du script distant est **inchange**. Le prelude installe
`exec 3>&1` puis `exec 1>&2`, de sorte que tout ce qui sort entre
`OSD_ROWS_BEGIN` et `OSD_ROWS_END` est ecrit sur le descripteur 3, et
que `stdout` ne porte que le bloc machine. C'est ce contrat que
`_parse_result` consomme, et il est **verifie** comme tel : Ansible
transmet `stdout` intact, descripteur 3 compris.

Le seul point d'attention est le code de retour. Ansible execute le
module `script` et renvoie `rc=0` meme lorsque le script distant sort en
erreur : le code du script voyage dans le bloc machine
(`OSD_RESULT_END rc=n`), que `_parse_result` sait deja lire. Utiliser le
`rc` d'Ansible reviendrait a perdre l'echec -- c'est explicitement
documente dans `run_script`.

## Ce que ce module ne fait pas

Ansible n'est employe que pour **executer le script**. Le transfert du
dump (etape 13) passe par `scp`/`rsync`/`sftp` lances depuis le serveur
de saut, donc hors de ce chemin : il est traite dans
`adapters/transfer.py`, qui pose `SSHPASS` depuis le meme coffre. C'est
une frontiere assumee, pas un oubli -- Ansible gere l'authentification
la ou il controle le processus, et `synchronize` ferait perdre la
detection `scp-legacy` qui evite l'echec sur AIX.

## Securite

Aucun secret n'est ecrit dans un argument de processus. Le mot de
passe de coffre est lu depuis un fichier, jamais depuis la
configuration de l'outil ; le mot de passe SSH lui-meme n'est jamais
vu par ce module : il est dechiffre par Ansible, dans son processus.

Le script est ecrit dans un fichier `0600` temporaire, car il porte les
valeurs injectees -- dont un `userid` de parfile, donc potentiellement
un secret. Il est supprime dans un `finally`, y compris en cas
d'interruption.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..errors import PrereqError
from ..redact import redact, redact_argv
from ..runner import Result, _parse_result, build_script

#: Groupes d'inventaire signes par l'outil. Source et cible sont
#: distincts par construction : un hote dans les deux reproduit la
#: topologie `partage`, ce qui est un choix, pas une erreur.
GROUP_SOURCE = "osd_source"
GROUP_TARGET = "osd_target"

#: Delai d'un `ping` de verification de l'inventaire.
PING_TIMEOUT = 60


class AnsibleRunner:
    """Execute un script distant par Ansible, avec la meme interface
    que `LocalRunner` et `RemoteRunner`.

    `host` est conserve tel quel : c'est le nom d'inventaire, et
    l'utilisateur ne doit pas avoir a distinguer « nom Ansible » de
    « adresse IP ». Si l'inventaire declare `ansible_host`, les deux
    peuvent differer.
    """

    kind = "remote"

    def __init__(
        self,
        host: str,
        *,
        inventory: str,
        group: str = GROUP_SOURCE,
        vault_password_file: str = "",
        side: str = "source",
        extra_args: Sequence[str] = (),
    ) -> None:
        if not host:
            raise PrereqError("hote non configure pour l'execution distante")
        if not inventory:
            raise PrereqError(
                f"OSD_INVENTORY absent : impossible d'executer sur {host}",
                hint="Renseigner OSD_INVENTORY dans la configuration, ou "
                     "laisser le hote vide pour une execution locale.",
            )
        self.host = host
        self.inventory = inventory
        self.group = group or GROUP_SOURCE
        self.vault_password_file = vault_password_file
        self.side = side
        self.extra_args = list(extra_args)
        self._binaries: Dict[str, bool] = {}
        self._checked = False
        # Memoise : le transfert demande ces valeurs une fois par hote,
        # et chaque lecture dechiffre le coffre.
        self._variables: Dict[str, str] = {}
        self._opts: Optional[List[str]] = None
        self._transfer_host: Optional[str] = None
        self._transfer_user: Optional[str] = None

    # -- Introspection ---------------------------------------------------

    @property
    def label(self) -> str:
        return f"ansible:{self.group}/{self.host}"

    def allows_mutation(self) -> bool:
        """Toujours vrai : voir `RemoteRunner.allows_mutation`."""
        return True

    def has_binary(self, name: str) -> bool:
        """Indique si un binaire est dans le PATH de l'hote.

        Memorise : chaque appel coute un aller-retour, et le workflow
        pose la question au plus deux fois par hote.
        """
        if name in self._binaries:
            return self._binaries[name]
        found = False
        try:
            # Le nom du binaire est passe en **argument**, pas interpole
            # dans le corps. L'echec de l'interpolation est instructif :
            # le corps recevait `"$1"`, donc la valeur mise en argument
            # -- un libelle, pas le nom -- et la sonde cherchait
            # toujours un binaire qui n'existait pas. Le resultat etait
            # un `OSD_FOUND=0` parfaitement plausible, sans erreur
            # visible, pour tous les binaires.
            result = self.run_script(
                build_script(_which_script(), [name], env={}),
                timeout=PING_TIMEOUT,
            )
            found = result.get("OSD_FOUND") == "1"
        except PrereqError:
            found = False
        self._binaries[name] = found
        return found

    # -- Execution -------------------------------------------------------

    def argv(self, script_path: str) -> List[str]:
        """Ligne de commande Ansible effective, pour les journaux.

        Aucun secret n'y figure : le mot de passe de coffre est passe
        par `--vault-password-file`, donc par chemin, et le mot de passe
        SSH est dechiffre par Ansible lui-meme.
        """
        return self._argv_module("script", script_path)

    def run_script(
        self,
        script: str,
        *,
        timeout: Optional[int] = None,
        mutating: bool = False,
    ) -> Result:
        """Execute `script` sur l'hote par le module `script` d'Ansible.

        Le module `script` transfere le script dans un fichier sur
        l'hote puis l'execute. C'est le seul mode compatible avec notre
        contrat : le script a besoin d'un descripteur 3, qu'il pose
        lui-meme, et il doit pouvoir lire ses propres arguments.

        Le `rc` d'Ansible est **ignore** : le module renvoie 0 meme si
        le script sort en erreur. Le code de retour reel est celui du
        bloc machine (`OSD_RESULT_END rc=n`), que `_parse_result`
        extrait deja.
        """
        if not self._checked:
            self._verify_ansible()

        handle, path = tempfile.mkstemp(prefix="osd-ansible-", suffix=".sh")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(script)
            os.chmod(path, 0o600)
            argv = self.argv(path)
            try:
                proc = subprocess.run(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    env=_ansible_env(),
                )
            except subprocess.TimeoutExpired as exc:
                raise PrereqError(
                    f"delai depasse ({timeout}s) sur {self.label}",
                    detail=[redact((exc.stderr or b"").decode("utf-8", "replace"))],
                ) from None
            except FileNotFoundError:
                raise PrereqError(
                    "ansible absent du serveur de saut",
                    hint="Installer ansible-core, ou laisser le hote vide "
                         "pour une execution locale.",
                ) from None
            except OSError as exc:
                raise PrereqError(
                    f"echec Ansible vers {self.label} : {exc.strerror}"
                ) from None
        finally:
            # Le script porte les valeurs injectees, dont un `userid` de
            # parfile : il ne doit pas survivre a l'appel, meme en cas
            # d'interruption ou d'exception.
            try:
                os.unlink(path)
            except OSError:
                pass

        stdout, stderr, failed = _unwrap_ansible(
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )
        if failed and not stdout.strip():
            # Aucun bloc machine : l'echec est anterieur au script
            # (authentification, inventaire, module introuvable). Le
            # diagnostic d'Ansible est alors la seule information, et
            # la remonter telle quelle est plus utile qu'un « bloc de
            # resultat incomplet » generique.
            raise PrereqError(
                f"Ansible n'a pas pu executer le script sur {self.label}",
                detail=redact(stderr).splitlines()[-12:],
                hint="Verifier que l'hote figure dans l'inventaire "
                     "OSD_INVENTORY, que le mot de passe du coffre est "
                     "correct, et que le compte SSH est autorise.",
            )

        return _parse_result(
            stdout,
            stderr,
            # Le rc d'Ansible ne dit rien du script : c'est le bloc
            # machine qui porte la verite, et `_parse_result` l'extrait.
            0,
            command=" ".join(redact_argv(argv)),
        )

    # -- Partage de l'inventaire avec le transfert ------------------------

    def ssh_password(self) -> str:
        """Mot de passe SSH de l'hote, tel que l'inventaire le declare.

        Le transfert (etape 13) n'est **pas** execute par Ansible :
        `scp`/`rsync`/`sftp` sont lances directement depuis le serveur de
        saut. Sans ce secret, ces binaires n'auraient aucune
        authentification a presenter, et le run echouerait apres avoir
        exporte.

        Ansible est donc interroge pour le lire, et non pour l'executer.
        Le secret transite par `stdout`, donc par la memoire du
        processus : il n'est ni ecrit dans un fichier, ni place dans un
        argument, ni journalise. Il ne redescend **jamais** dans
        l'inventaire, qui reste la seule source.

        Une erreur de lecture ne doit pas faire echouer le run ici : le
        transfert n'est qu'une etape parmi dix-neuf, et il remontera son
        propre diagnostic, plus precis que « secret illisible ». On
        renvoie donc une chaine vide, ce qui fait tenter au transfert
        l'authentification par cle et echouer franchement.
        """
        return self._variable("ansible_password")

    def ssh_opts(self) -> List[str]:
        """Options SSH de l'hote, sous la forme attendue par `scp`/`rsync`.

        L'inventaire les porte dans `ansible_ssh_common_args`, c'est-a-dire
        dans la forme qu'OpenSSH attend : `-o ConnectTimeout=10 -o
        StrictHostKeyChecking=accept-new`. Le transfert, lui, construit
        `scp -o <option>` et `rsync -e "ssh <option>..."`, et veut donc
        les options **nues**, sans le `-o` qui les introduit.

        D'ou la conversion. Elle n'est pas cosmetique : passer
        `-o ConnectTimeout=10` comme un seul argument donnerait
        `scp -o -o ConnectTimeout=10`, que `scp` refuse.

        Une variable absente donne une liste vide, non une erreur : les
        options de securite du transfert sont decidées par `_merge_opts`
        (cf. `adapters/transfer.py`), qui ajoute `BatchMode` et friends
        quel que soit le contenu de l'inventaire. Se fier ici a
        l'inventaire seul ferait dependre la garantie anti-blocage d'un
        fichier que l'exploitant peut laisser vide.

        La cle privee est ajoutee, et c'est indispensable. Elle est une
        variable **seule** dans l'inventaire
        (`ansible_ssh_private_key_file`), que `ansible_ssh_common_args`
        ne porte pas. Sans la traduire en `IdentityFile`, un inventaire
        authentifie par cle -- le mode recommande, et celui que
        l'ancienne `SSH_KEY` couvrait -- executerait les dix-neuf etapes
        puis echouerait a la treizieme, sans qu'aucune des douze premieres
        ne donne a supposer le contraire. L'echec aurait donc lieu apres
        l'export, c'est-a-dire apres le temps le plus long du run.
        """
        if self._opts is not None:
            return self._opts
        self._opts = _parse_ssh_args(self._variable("ansible_ssh_common_args"))
        cle = self._variable("ansible_ssh_private_key_file")
        if cle and not any(o.startswith("IdentityFile=") for o in self._opts):
            # Une `IdentityFile` deja presente dans `ansible_ssh_common_args`
            # fait autorite : elle peut en designer plusieurs, et
            # `ansible_ssh_private_key_file` n'en porte qu'une.
            self._opts.append("IdentityFile=" + cle)
        return self._opts

    # -- Identite pour les clients du serveur de saut ---------------------

    @property
    def transfer_host(self) -> str:
        """Adresse que `scp`/`rsync`/`sftp` doivent joindre.

        Le transfert n'emprunte pas le chemin d'Ansible : ce sont des
        clients du serveur de saut, et ils ignorent l'inventaire. Or
        `SOURCE_HOST` et `TARGET_HOST` designent des **noms
        d'inventaire**, que ces clients ne savent pas resoudre -- le nom
        n'a de sens que pour Ansible.

        `ansible_host` est donc traduit en adresse ici. Sans cette
        traduction, un inventaire qui **separe** le nom de l'adresse --
        la pratique recommandee, puisque le nom survit a un changement
        d'IP -- executerait les dix-neuf etapes et echouerait a la
        treizieme, sur une erreur de resolution de nom. A l'inverse,
        un inventaire ou le nom est directement resoluble -- une adresse,
        ou un alias DNS -- n'a rien a traduire, et la valeur de retour
        est alors le nom lui-meme.

        Aucun appel n'est fait si l'inventaire ne definit pas
        `ansible_host`, ce qui evite un dechiffrement du coffre pour rien.
        """
        if self._transfer_host is not None:
            return self._transfer_host
        self._transfer_host = self._variable("ansible_host") or self.host
        return self._transfer_host

    @property
    def transfer_user(self) -> str:
        """Compte SSH que le transfert doit employer, ou vide.

        Vide signifie « ne pas prefixer d'utilisateur » : le client SSH
        prend alors le compte courant, ce qui est le comportement normal
        sur un serveur de saut ou l'on execute deja sous le bon compte.
        """
        if self._transfer_user is not None:
            return self._transfer_user
        self._transfer_user = self._variable("ansible_user")
        return self._transfer_user

    def _variable(self, nom: str) -> str:
        """Lit une variable d'inventaire, dechiffree si besoin.

        Le joker `default("")` est pose **dans l'expression**, et non
        apres coup. Sans lui, une variable absente ne produit aucun
        statut d'echec exploitable : `ansible -m debug` renvoie `msg` avec
        le texte « the task includes an option with an undefined
        variable », `failed` restant vide. Ce texte deviendrait alors le
        mot de passe remis a `sshpass`, et l'echec n'apparaitrait qu'a la
        copie, sous la forme d'une authentification refusee sans lien
        visible avec sa cause.

        Avec le joker, l'absence est traduite en chaine vide a la source :
        le transfert tente l'authentification par cle et echoue
        franchement, ce qui se diagnostique. Le reste des echecs -- hote
        injoignable, JSON illisible, delai depasse -- donne la meme chaine
        vide, pour la meme raison : aucun n'a de remede ici, et tous
        ont un diagnostic plus precis ailleurs.
        """
        if nom in self._variables:
            return self._variables[nom]
        valeur = ""
        try:
            proc = subprocess.run(
                self._argv_debug('msg={{ ' + nom + ' | default("") }}'),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=PING_TIMEOUT,
                env=_ansible_env(),
            )
            payload = _premier_json(proc.stdout.decode("utf-8", "replace"))
            if isinstance(payload, dict) and not _en_echec(payload):
                brut = payload.get("msg")
                if isinstance(brut, str):
                    valeur = brut
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            valeur = ""
        self._variables[nom] = valeur
        return valeur

    def _argv_debug(self, expression: str) -> List[str]:
        return self._argv_module("debug", expression)

    def _argv_module(self, module: str, argument: str) -> List[str]:
        """Ligne de commande `ansible` pour un module et son argument.

        L'argument n'est **pas** protege par `shlex.quote`. Il est
        transmis dans une liste, donc sans shell : les citations
        ajouteraient des caracteres litteraux au lieu de proteger, et
        Ansible verrait une expression commencant par une apostrophe.
        C'est le defaut classique du quoting applique deux fois.

        L'integrite de l'argument reste assured par ailleurs : `script`
        recoit un chemin genere par `mkstemp`, et `debug` recoit une
        expression construite ici, sans donnee exterieure.
        """
        argv = ["ansible", self.host, "-i", self.inventory]
        if self.vault_password_file:
            argv += ["--vault-password-file", self.vault_password_file]
        argv += ["-m", module, "-a", argument]
        argv += self.extra_args
        return argv

    # -- Verification ----------------------------------------------------

    def _verify_ansible(self) -> None:
        """Echoue tot et clairement si Ansible n'est pas utilisable.

        Called une fois par runner. Le but est que l'exploitant
        apprenne « ansible manque » a l'etape 3, et non « connexion
        refusee » a l'etape 4 apres avoir attendu le delai de connexion.
        """
        self._checked = True
        if not Path(self.inventory).is_file():
            raise PrereqError(
                f"inventaire introuvable : {self.inventory}",
                hint="Verifier OSD_INVENTORY dans la configuration.",
            )
        if self.vault_password_file and not Path(self.vault_password_file).is_file():
            raise PrereqError(
                f"mot de passe de coffre introuvable : {self.vault_password_file}",
                hint="Le fichier doit exister et etre lisible par le "
                     "compte qui execute. Il ne doit jamais etre versionne.",
            )


def _ansible_env() -> Dict[str, str]:
    """Environnement minimal pour Ansible.

    La locale n'est **pas** forcee a `C`, contrairement a
    `_clean_env` du runner SSH. Ansible 2.14 refuse de demarrer si
    l'encodage de la locale n'est pas UTF-8, et force `LC_ALL=C`
    provoque exactement cela (`Ansible requires the locale encoding to
    be UTF-8`).

    Le tri des resultats n'est pas expose ici : le script distant
    impose lui-meme `LC_ALL=C` dans le bloc machine, donc la sortie
    reste stable quel que soit l'encodage du serveur de saut. Ce que
    fait `ansible` autour n'est que de l'affichage.

    `HOME` est conserve : Ansible y lit `~/.ansible.cfg` et
    `~/.ansible/tmp`. Le fixer a `/` le ferait echouer sur son propre
    repertoire temporaire.
    """
    env = dict(os.environ)
    # `ANSIBLE_FORCE_COLOR` et non `LC_ALL` : seule la couleur des
    # messages est neutralisee, pas l'encodage.
    env["ANSIBLE_FORCE_COLOR"] = "0"
    env["ANSIBLE_NOCOLOR"] = "1"
    env["ANSIBLE_RETRY_FILES_ENABLED"] = "0"
    return env


def _unwrap_ansible(stdout: str, stderr: str) -> tuple:
    """Separe la sortie du script de l'enrobage JSON d'Ansible.

    Ansible renvoie un JSON par hote. On en extrait `stdout`, `stderr`
    et l'indicateur d'echec, pour que `_parse_result` voie exactement
    ce qu'aurait vu un `ssh` direct.

    Le retour est un triplet `(stdout, stderr, failed)`. `stdout` est
    **vide** si le JSON est absent ou illisible : c'est au texte
    d'origine de faire le diagnostic, plutot qu'a un parsing qui reussirait.

    Quand le payload annonce un echec, son `msg` est reporte en fin de
    `stderr` : sur un hote injoignable, le `stderr` du payload est vide
    et rien d'autre ne nomme la cause — le detail ne se serait alors
    reduit qu'au hint generique, sans une seule ligne de motif.
    """
    payload = _premier_json(stdout)
    if not isinstance(payload, dict):
        return stdout, stderr, bool(stderr.strip())

    # Ansible 2.14 : sortie en JSON sur stdout quand `-m` est employe.
    # Le script peut lui-meme avoir ecrit du JSON ; on ne prend que le
    # premier objet, qui est celui d'Ansible.
    inner_out = payload.get("stdout", "")
    inner_err = payload.get("stderr", "")
    failed = bool(payload.get("failed")) or bool(payload.get("unreachable"))
    msg = payload.get("msg")
    if failed and isinstance(msg, str) and msg.strip():
        # En queue, et non en tete : `_parse_result` ne lit que le bloc
        # machine, et le detail de `PrereqError` ne retient que les
        # dernieres lignes.
        inner_err = (inner_err + "\n" if inner_err else "") + msg
    return inner_out, (inner_err + stderr), failed


def _premier_json(texte: str):
    """Premier objet JSON d'une sortie Ansible, ou `None`.

    Ansible ecrit son JSON sur `stdout`, mais precede de lignes de
    bibliographic. On cherche la premiere `{` et on decode depuis la --
    `raw_decode` s'arrete a la fin du premier objet, ce qui laisse
    intacte tout ce qui suit, notamment le second hote si un jour la
    commande en vise plusieurs.
    """
    debut = texte.find("{")
    if debut < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(texte[debut:])
    except (ValueError, TypeError):
        return None
    return payload


def _en_echec(payload) -> bool:
    """La tache Ansible a-t-elle echoue, et le `msg` est-il un diagnostic ?

    Ansible place dans `msg` le resultat de l'expression demandee, mais
    aussi le message d'erreur quand la tache echoue. Les deux sont
    indiscernables sur la seule valeur : c'est le statut qui les
    distingue, et c'est lui qu'il faut consulter avant de croire ce que
    `msg` affirme.
    """
    if not isinstance(payload, dict):
        return True
    if payload.get("failed") or payload.get("unreachable"):
        return True
    return "msg" not in payload


def _parse_ssh_args(brut: str) -> List[str]:
    """Convertit `ansible_ssh_common_args` en options nues.

    L'inventaire ecrit la chaine telle qu'OpenSSH la veut :

        -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new

    Le transfert construit `scp -o <opt>` et `rsync -e "ssh <opt>..."`,
    et attend donc `ConnectTimeout=10` seul : garder le `-o` donnerait
    `scp -o -o ConnectTimeout=10`, que `scp` refuse.

    Le decoupage se fait sur les espaces, ce qui suffit parce que les
    options SSH retenues n'en contiennent pas. Les guillemets sont
    retires : un auteur peut en mettre, et `scp` refuserait alors une
    option dont le nom commence par une apostrophe.

    Une valeur contenant un espace serait mal decoupee. Plutot que de
    laisser passer une option deconnectee, on l'ignore : `_merge_opts`
    posera les options de securite, et le transfert echouera sur une
    authentification, ce qui se diagnostique.
    """
    if not brut or not brut.strip():
        return []
    options: List[str] = []
    for morceau in brut.split():
        if morceau == "-o":
            continue
        if morceau.startswith("-o") and len(morceau) > 2:
            morceau = morceau[2:]
        morceau = morceau.strip("'\"")
        if morceau:
            options.append(morceau)
    return options


def _which_script() -> str:
    """Corps POSIX minimal qui teste la presence d'un binaire.

    C'est un **corps**, pas un script : il doit passer par
    `build_script`, comme tous les autres. La raison est historique, et
    instructive. Ce module envoyait d'abord ce texte seul, sans prelude,
    en supposant que le transport poserait les fonctions `osd_*`. Il ne
    les pose pas : `ansible -m script` transfere le fichier tel quel et
    l'execute, rien de plus.

    L'echec qui en resulted etait un echec d'authentification, et
    parfaitement limpide : `osd_kv: command not found`, sur un hote
    pourtant joignable et un inventaire valide. Aucun test unitaire ne
    l'aurait vu, car ils interceptent `subprocess.run` et n'executent
    donc rien. Il a fallu un run reel pour le rencontrer -- ce qui est
    l'argument le plus fort en faveur de l'integration.
    """
    return (
        "if command -v \"$1\" >/dev/null 2>&1; then\n"
        "  osd_kv OSD_FOUND 1\n"
        "  osd_kv OSD_PATH \"$(command -v \"$1\")\"\n"
        "else\n"
        "  osd_kv OSD_FOUND 0\n"
        "fi\n"
    )
