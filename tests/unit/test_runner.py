"""Tests du protocole d'execution distante.

C'est la couche la plus exposee du projet : elle envoie du texte a un
shell, sur une machine qu'elle ne controle pas, pour le compte d'une
base de production. Une erreur ici ne se manifeste pas par un echec
propre mais par une **degradation silencieuse** : un script qui ne
tourne pas sur le Bourne shell d'AIX, un quoting qui perd un octet d'un nom de
fichier, une variable d'environnement posee apres le prelude et donc
sans effet.

La strategie de test est donc de ne jamais se contenter de comparer des
chaines. Chaque propriete de securite est verifiee **par execution** :

* le quoting est prouve en faisant relire l'argument par un vrai
  `/bin/sh`, pas en comparant la chaine construite a une chaine
  attendue ;
* l'inertie d'une injection est prouvee en verifiant qu'un argument
  hostile est **recupere litteralement** par le corps du script, ce qui
  est impossible s'il a ete interprete ;
* la validite des scripts est prouvee par `sh -n` sur le script
  complet, prelude compris, et pas sur le corps seul ;
* la portabilite AIX est verifiee mecaniquement, par l'absence des
  constructions que le prelude interdit.

Aucun de ces tests n'a besoin de base ni de reseau : ils font partie
des tests d'integration, mais ils sont plus rapides et plus precoces.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from support import SRC_DIR

from osd import exit_codes as ec
from osd import runner
from osd.errors import OsdError, PrereqError
from osd.runner import (
    LocalRunner,
    Raw,
    RemoteProtocolError,
    RemoteRunner,
    Result,
    build_script,
    load_body,
)

#: Tous les corps de scripts distants, prelude compris.
CORPS = sorted(p.name for p in (SRC_DIR.parent / "shell").glob("*.sh"))


#: Apostrophe, factorisee : la citer dans une chaine Python demande
#: de l'ecrire `chr(39)`, et un `'...'` mal place casse le fichier
#: entier au lieu d'une assertion.
APOSTROPHE = "'"


def sh_quote(valeur: str) -> str:
    return runner._single_quote(valeur)


#: Valeurs qui, non quotees, casseraient un script ou pire.
VALEURS_HOSTILES = [
    "",
    "simple",
    "avec espace",
    "avec\ttabulation",
    "avec\nsaut-de-ligne",
    "guillemet'simple",
    'double"guillemet',
    "retour\\antislash",
    "`backtick`",
    "$(id)",
    "${HOME}",
    "$HOME",
    "*",
    "?",
    "~",
    "a;b",
    "a|b",
    "a&&b",
    "a||b",
    "; rm -rf /tmp/xyz",
    "'; touch /tmp/xyz.pwned; echo '",
    '"; touch /tmp/xyz.pwned; echo "',
    "$(touch /tmp/xyz.pwned)",
    "`touch /tmp/xyz.pwned`",
    "'; osd_die 0; echo '",
    "%s",
    "\\",
    "'",
    "''",
    "a" * 500,
    "éàü",
    "chemin/fichier",
    "ligne1\nligne2\nligne3",
]


def _temoin_inexistant() -> Path:
    return Path(tempfile.gettempdir()) / "xyz.pwned"


def _valeur_relavee(valeur: str) -> str:
    """Fait relire `valeur` par un vrai shell, a travers tout le protocole.

    Le chemin emprunté est exactement celui de la production :
    `build_script` produit `osd_arg1=<quote>`, le script la recombine
    par `set --`, le corps la lit dans `"$1"`, et le prelude la
    restitue sur le descripteur 3. Rien n'est simule.

    C'est cette chaine — et non une comparaison de chaines Python —
    qui prouve le quoting : si `_single_quote` avait un defaut, le
    shell le rendrait visible ici, exactement comme sur l'hote AIX.

    Le corps ecrit sur le descripteur 3 parce que le prelude a
    redirige stdout (1) vers stderr : c'est 3 qui porte le canal
    machine. Ecrire sur stdout, c'est ecrire dans les journaux.
    """
    resultat = LocalRunner().run_script(
        build_script('printf %s "$1" >&3', [valeur]), timeout=60
    )
    lignes = resultat.stdout_raw.splitlines()
    try:
        debut = lignes.index("OSD_RESULT_BEGIN") + 1
    except ValueError:  # pragma: no cover - le prelude emet toujours BEGIN
        return resultat.stdout_raw
    fin = len(lignes)
    for position in range(len(lignes) - 1, debut - 1, -1):
        if lignes[position].startswith("OSD_RESULT_END"):
            fin = position
            break
    # Un saut de ligne **interieur** fait partie de la valeur : seul
    # celui qui precedait le marqueur est un separateur de protocole.
    return "\n".join(lignes[debut:fin])


class TestQuoting(unittest.TestCase):
    """`_single_quote` est prouve par relecture dans un vrai shell."""

    def test_aller_retour_fidele(self):
        """Chaque valeur hostile doit revenir **exactement** identique.

        Comparer la chaine construite a une chaine attendue
        prouverait seulement que la fonction fait ce qu'on suppose. La
        faire relire par `/bin/sh`, a travers le protocole complet,
        prouve qu'un shell POSIX en fait aussi — ce qui est la seule
        propriete qui compte, puisque c'est le `sh` de l'hote qui lira
        la ligne.

        Les valeurs multilignes, accentuées et non-ASCII sont
        indispensables : l'outil travaille sur des noms de schemas, des
        chemins de DIRECTORY et des noms de logfiles dont il ne maitrise
        pas les caracteres.
        """
        for valeur in VALEURS_HOSTILES:
            with self.subTest(valeur=repr(valeur)[:60]):
                self.assertEqual(_valeur_relavee(valeur), valeur)

    def test_le_donneur_de_tiret_ne_peut_pas_etre_confondu(self):
        """Une valeur qui commence par un tiret doit rester un argument.

        Sans les simples quotes, `osd_arg1=--help` ferait que le corps
        lise `$1` comme l'option d'un binaire, et l'appel porterait sur
        une option et non sur la donnee. C'est le piege classique du
        quoting par espace.
        """
        self.assertEqual(_valeur_relavee("--help"), "--help")
        self.assertEqual(_valeur_relavee("-rf /"), "-rf /")

    def test_un_apostrophe_produit_cinq_quotes_et_un_backslash(self):
        """Le mecanisme d'echappement est verifie **par son compte**.

        `_single_quote` produit la chaine fermante, un backslash, la
        chaine vide, puis la reopenante. Le compte de cinq apostrophes
        et d'un backslash est ce qui prouve ce mecanisme : une citation
        qui en produirait quatre aurait fonctionne sur cet exemple et
        casse sur le suivant, ou l'apostrophe est suivie d'un separateur
        de mot.
        """
        cite = sh_quote("a" + APOSTROPHE + "b")
        self.assertEqual(cite, "'a'\\''b'")
        self.assertEqual(cite.count(APOSTROPHE), 5)
        self.assertEqual(cite.count(chr(92)), 1)

    def test_deux_apostrophes_consecutives(self):
        """Le cas qui casse les citations « simplistes ».

        Une implementation qui remplace chaque apostrophe par deux
        apostrophes produirait ici quatre apostrophes, que le shell lit
        comme la chaine vide suivie d'un mot nu : la valeur
        reviendrait vide, silencieusement.
        """
        for valeur in ("a" + APOSTROPHE * 2 + "b", APOSTROPHE,
                       APOSTROPHE * 2, APOSTROPHE * 3):
            with self.subTest(valeur=repr(valeur)):
                self.assertEqual(_valeur_relavee(valeur), valeur)

    def test_une_valeur_vide_reste_vide(self):
        self.assertEqual(_valeur_relavee(""), "")

    def test_les_blancs_ne_sont_pas_reduits(self):
        """Les blancs internes font partie de la donnee.

        Un chemin de `DIRECTORY` contenant deux espaces doit revenir
        avec ses deux espaces, sinon le script distant irait chercher
        un repertoire qui n'existe pas et l'erreur serait `ORA-12154` —
        une accusation de reseau pour une cause locale.
        """
        for valeur in ("/opt/oracle/my  dir", "a\tb", "  LEADING",
                       "TRAILING  ", "milieu   espaces"):
            with self.subTest(valeur=repr(valeur)):
                self.assertEqual(_valeur_relavee(valeur), valeur)

    def test_un_chemin_absent_ne_devient_pas_un_glob(self):
        """`*` est un motif de fichier, pas une lettre.

        Sans les simples quotes, un chemin contenant `*` serait
        developpe par le shell en une liste de fichiers, et la variable
        contiendrait plusieurs mots — donc le chemin reel serait
        reconstruit a partir du seul premier element de la liste.
        """
        self.assertEqual(_valeur_relavee("/opt/*/dpdump"), "/opt/*/dpdump")
        self.assertEqual(_valeur_relavee("?"), "?")
        self.assertEqual(_valeur_relavee("~"), "~")


class TestBuildScript(unittest.TestCase):
    def test_un_argument_au_moins_est_exige(self):
        """Sans argument, `"$@"` designe l'ensemble des arguments de `sh -s`.

        Le corps verrait des arguments que le Python n'a jamais poses,
        et un `osd_kv OSD_X "$1"`_emitterait une valeur vide au lieu
        d'echouer franchement.
        """
        with self.assertRaises(ValueError):
            build_script("exit 0", [])

    def test_le_script_complet_est_syntactiquement_valide(self):
        """`sh -n` sur l'assemblage complet, prelude compris.

        Le prelude et le corps sont ecrits separement mais n'ont de
        sens qu'ensemble : le `trap`, `osd_exit` et la redirection
        `exec 1>&2` ne sont interpretables qu'une fois concatenees.
        Verifier le corps seul laisserait passer un script qui ne
        compile qu'isole.
        """
        for nom in CORPS:
            if nom == "prelude.sh":
                continue  # le prelude n'a pas d'arguments
            with self.subTest(corps=nom):
                script = build_script(load_body(nom), ["valeur hostile"])
                proc = subprocess.run(
                    ["/bin/sh", "-n"], input=script.encode("utf-8"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_les_arguments_hostiles_ne_cassent_pas_la_syntaxe(self):
        for nom in ("remote_which.sh", "remote_exec.sh", "remote_space.sh",
                    "remote_sqlplus.sh", "remote_pathinfo.sh",
                    "remote_listdir.sh", "remote_datapump.sh"):
            for valeur in VALEURS_HOSTILES:
                with self.subTest(corps=nom, valeur=repr(valeur)[:40]):
                    script = build_script(load_body(nom), [valeur, valeur])
                    proc = subprocess.run(
                        ["/bin/sh", "-n"], input=script.encode("utf-8"),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=30,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_les_arguments_sont_numerotes_puis_recombines(self):
        """Le contrat est `osd_argN` + `set --`, pas une ligne de commande.

        C'est ce qui permet au corps d'utiliser `"$@"` comme un script
        normal, et c'est ce qui garantit qu'aucun argument n'apparait
        dans la ligne de commande du processus distant.
        """
        script = build_script("exit 0", ["un", "deux", "trois"])
        self.assertIn("osd_arg1='un'", script)
        self.assertIn("osd_arg2='deux'", script)
        self.assertIn("osd_arg3='trois'", script)
        self.assertIn('set -- "$osd_arg1" "$osd_arg2" "$osd_arg3"', script)

    def test_les_arguments_ne_sont_pas_reproduits_en_clair_a_cote(self):
        """Un secret en argument ne doit pas apparaitre deux fois.

        Le script etant journalise, une valeur posee en clair hors des
        simples quotes se retrouverait dans le journal a cote de la
        forme quotee. Les deux occurrences attendues — l'affectation et
        la reference — sont sans le secret.
        """
        secret = "MotDePasseSecret123"
        script = build_script("exit 0", [secret])
        # Une seule occurrence : celle de l'affectation quotee.
        self.assertEqual(script.count(secret), 1)
        self.assertIn("osd_arg1='MotDePasseSecret123'", script)

    def test_l_amorcage_est_place_avant_les_arguments(self):
        """`bootstrap` materialise un fichier qu'un `Raw` designe ensuite.

        L'ordre est impose par `set -u` : un `Raw` ne peut designer
        qu'une variable deja posee, donc l'amorcage doit preceder les
        arguments. Inverse, le script evaluait `osd_arg2="$osd_parpath"`
        avant que `osd_parpath` existe, et mourait — ce qui est
        exactement ce qui est arrive au parfile Data Pump, jamais
        observe parce que le dry-run retenait cette etape.
        """
        script = build_script(
            "exit 0", ["contenu"], bootstrap="osd_fichier=$osd_arg1"
        )
        self.assertLess(script.index("osd_fichier="), script.index("osd_arg1="))
        self.assertLess(script.index("osd_arg1="), script.index("# --- corps"))

    def test_un_raw_peut_designer_une_variable_de_l_amorcage(self):
        """Le motif reel du parfile Data Pump, verifie par execution.

        C'est le seul endroit du projet ou un `Raw` sert a autre chose
        qu'a passer une valeur deja connue, et c'est donc le seul ou un
        defaut d'ordonnancement pouvait se cacher. Le controle est donc
        une execution : la variable est relue par le corps.
        """
        resultat = LocalRunner().run_script(
            build_script(
                'osd_kv OSD_LU "$1"\nexit 0\n',
                [Raw('"$osd_parpath"')],
                bootstrap="osd_parpath=" + sh_quote("/tmp/osd/x.par"),
            ),
            timeout=60,
        )
        self.assertEqual(resultat.get("OSD_LU"), "/tmp/osd/x.par")

    def test_le_prelude_est_present_et_precede_le_corps(self):
        script = build_script("# corps\n", ["x"])
        self.assertLess(script.index("OSD_RESULT_BEGIN"), script.index("# corps"))
        self.assertIn('osd_exit', script)

    def test_le_trap_porte_le_code_explicitement_et_non_le_statut_du_shell(self):
        """Le code doit voyager par `osd_exit`, pas par `$?` developpe au trap.

        Sur le Bourne shell d'AIX, `$?` n'est pas mis a jour pour le trap
        de sortie : il y garde le statut de la derniere commande executee
        avant le `exit`, presque toujours 0. Un `trap 'osd_finish $?' 0`
        y annoncerait donc `rc=0` pour un echec, et l'analyseur ne pourrait
        plus distinguer une reussite d'un echec.
        """
        script = build_script("# corps\n", ["x"])
        self.assertIn("trap 'osd_finish $_osd_exit_code' 0", script)
        self.assertNotIn("trap 'osd_finish $?' 0", script)
        # `osd_exit` doit aussi etre le chemin de sortie de `osd_die`,
        # sinon un `osd_die` resterait muet sur le code.
        self.assertIn('osd_exit "${2:-1}"', script)

    def test_le_prelude_n_active_pas_set_u(self):
        """`set -u` est incompatible avec le Bourne shell d'AIX.

        Ce shell leve une erreur sur toute expansion d'un parametre non
        defini, y compris `${VAR:-defaut}`. Le prelude s'en abstient donc,
        et passe en `set +u` : c'est la seule forme qui ne casse pas les
        scripts sur AIX.
        """
        script = build_script("# corps\n", ["x"])
        self.assertIn("set +u", script)
        # `set -u` ne doit pas apparaitre comme instruction active : il ne
        # le serait que dans un commentaire ou une explication.
        actives = [l for l in script.split("\n")
                   if l.strip().startswith("set -u")]
        self.assertEqual(actives, [], f"`set -u` actif : {actives}")


class TestProfilDeConnexion(unittest.TestCase):
    """Le client Oracle est dans le `PATH` du profil, pas dans l'environnement.

    Ni Ansible ni `ssh` n'ouvrent une session de connexion : ce que nous
    lancons est une coquille non interactive, qui ne source ni
    `/etc/profile` ni `~/.profile`. Or c'est la que l'installeur Oracle
    depose `ORACLE_HOME/bin`. Sans sourcing explicite, `expdp` parait
    absent sur un hote ou il est installe — et l'etape 3 annonce un
    client manquant avec un remede, « verifier le `PATH` du compte »,
    auquel il n'y a rien a verifier puisque c'est justement le `PATH`
    de l'exploitant qui fait defaut.

    Ces tests passent par un vrai `sh`, avec un vrai `HOME` : c'est le
    seul moyen de savoir si le sourcing **marche**, plutot que s'il est
    ecrit.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.racine = Path(self.tmp.name)
        self.home = self.racine / "home"
        self.home.mkdir()
        self._precedent = os.environ.get("HOME")
        self.addCleanup(self._restaurer)
        os.environ["HOME"] = str(self.home)

    def _restaurer(self) -> None:
        if self._precedent is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._precedent

    def _profil(self, contenu: str) -> None:
        (self.home / ".profile").write_text(contenu, encoding="ascii")

    def _client(self, nom: str = "expdp") -> Path:
        """Un faux `expdp` dans un repertoire que seul le profil expose."""
        dossier = self.racine / "oracle-bin"
        dossier.mkdir(exist_ok=True)
        chemin = dossier / nom
        chemin.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        chemin.chmod(0o755)
        return chemin

    def _executer(self, corps: str) -> Result:
        return LocalRunner().run_script(
            build_script(corps, ["x"], env={}), timeout=120
        )

    def test_un_client_annonce_par_le_profil_est_resolu(self):
        """Le cas de production, verifie de bout en bout.

        Le profil *prepend* son chemin, comme le fait un `.profile` qui
        declare `PATH="$ORACLE_HOME/bin:$PATH"`. Le binaire resolu doit
        donc etre **celui du profil** — on compare le chemin obtenu, sans
        quoi le test passerait sur un `expdp` deja present dans
        l'environnement du developpeur, et ne verifierait rien.
        """
        attendu = self._client()
        self._profil(f'PATH="{attendu.parent}:$PATH"\n')
        resultat = self._executer(
            'osd_kv OU_TROUVE "$(command -v expdp 2>/dev/null || echo absent)"\n'
            "osd_finish 0\n"
        )
        self.assertEqual(resultat.get("OU_TROUVE"), str(attendu))

    def test_une_variable_non_initialisee_dans_le_profil_ne_bloque_pas(self):
        """Un profil qui lit une variable jamais definie ne bloque rien.

        Un profil n'ecrit pas toujours les variables qu'il initialise
        lui-meme : un `$LD_PRELOAD` ou un `$NLS_LANG` conditionnel laisse
        la variable non definie. Sous `set -u`, cela tuerait le script
        avant meme le prelude -- donc avant tout resultat, donc avant le
        moindre message : un echec muet, sans bloc de resultat a analyser.

        Le contournement retenu est general, pas un ordre de lignes :
        le prelude pose `set +u`, ce qui rend cette lecture inoffensive
        sur tous les shells.
        """
        self._profil('echo "NLS_LANG=$NLS_LANG_ABSENTE" >/dev/null 2>&1\n')
        resultat = self._executer("osd_kv VECU 1\nosd_finish 0\n")
        self.assertEqual(resultat.rc, 0, resultat.stderr)
        self.assertEqual(resultat.get("VECU"), "1")

    def test_un_profil_defaillant_n_interrompt_pas_le_script(self):
        """Un profil qui echoue ne doit pas faire echouer le run.

        Un profil peutemployer une construction propre a sa coquille de
        connexion, ou refermer sur un `return` invalide en `sh`. Rien de
        cela ne concerne notre execution, et une source non protegee
        transformerait chaque script en echec, avec un message qui
        designerait le profil et non le client absent.
        """
        self._profil("syntaxe ( ) invalide\nfalse\n")
        resultat = self._executer("osd_kv VECU 1\nosd_finish 0\n")
        self.assertEqual(resultat.rc, 0, resultat.stderr)
        self.assertEqual(resultat.get("VECU"), "1")

    def test_l_absence_de_tout_profil_est_le_cas_normal(self):
        """Aucun des fichiers n'est obligatoire.

        Un hote dont le client est deja dans le `PATH` herite n'a rien a
        charger. Le bloc doit donc etre neutre quand il ne trouve rien,
        et ne pas devenir une condition d'arret -- ce que le `|| :` de la
        boucle garantit, le dernier `export PATH` réussissant toujours.
        """
        resultat = self._executer("osd_kv VECU 1\nosd_finish 0\n")
        self.assertEqual(resultat.rc, 0, resultat.stderr)
        self.assertEqual(resultat.get("VECU"), "1")

    def test_le_path_du_profil_est_reellement_exporte(self):
        """Un profil qui pose `PATH` sans l'exporter ne change rien.

        C'est un defaut de redaction courant dans un `.profile`, et il
        est **invisible** a l'oeil : la variable est posee, l'export
        manque, et les commandes qui suivent la source -- les nôtres --
        n'en voient pas l'effet. Le client reste introuvable, et rien dans
        le script n'indique pourquoi.
        """
        dossier = self.racine / "sans-export"
        dossier.mkdir()
        (dossier / "expdp").write_text("#!/bin/sh\n", encoding="ascii")
        (dossier / "expdp").chmod(0o755)
        self._profil(f'PATH="{dossier}:$PATH"\n')  # pas de export
        resultat = self._executer(
            'osd_kv OU_TROUVE "$(command -v expdp 2>/dev/null || echo absent)"\n'
            "osd_finish 0\n"
        )
        self.assertEqual(resultat.get("OU_TROUVE"), str(dossier / "expdp"))


class TestInjection(unittest.TestCase):
    """Le point non negociable : un argument n'est jamais du code."""

    def setUp(self):
        self.temoin = _temoin_inexistant()
        if self.temoin.exists():  # pragma: no cover
            self.temoin.unlink()
        self.addCleanup(
            lambda: self.temoin.unlink() if self.temoin.exists() else None
        )

    def test_un_argument_hostile_arrive_litteralement_dans_le_corps(self):
        """Preuve directe : le corps **retrouve** la chaine, inchangee.

        Si l'argument avait ete interprete, le corps verrait une
        sous-chaine et le fichier temoin existerait. Les deux assertions
        sont faites : l'une prouve la fidelite, l'autre prouve
        l'absence d'execution.
        """
        for valeur in VALEURS_HOSTILES:
            if "touch" not in valeur:
                continue
            with self.subTest(valeur=repr(valeur)[:60]):
                self.assertEqual(_valeur_relavee(valeur), valeur)
                self.assertFalse(
                    self.temoin.exists(),
                    f"l'argument a ete interprete : {valeur!r}",
                )

    def test_un_argument_ne_peut_pas_ajouter_une_instruction(self):
        """Un `\n` dans un argument ne peut pas ajouter une instruction.

        Le prelude n'utilise ni `set -u` ni `set -e` : une instruction
        supplementaire s'executerait sans erreur, et poserait une
        variable supplementaire dans le bloc. C'est la forme
        d'injection la plus simple, et la seule qui ne passe par aucun
        caractere d'echappement.

        La valeur est relue par le helper et non par `osd_kv`, qui
        refuse par design les valeurs multilignes : ici, la Presence du
        saut de ligne est justement ce qu'on verifie.
        """
        resultat = LocalRunner().run_script(
            build_script(
                'printf %s "$1" >&3\nexit 0\n',
                ["valeur\nosd_kv OSD_INJECTE 1\nautre"],
            ),
            timeout=60,
        )
        self.assertEqual(_valeur_relavee("valeur\nosd_kv OSD_INJECTE 1\nautre"),
                         "valeur\nosd_kv OSD_INJECTE 1\nautre")
        # Aucune trace de l'instruction fantome dans le bloc machine.
        self.assertEqual(resultat.get("OSD_INJECTE", ""), "")
        self.assertEqual(len(resultat.rows), 0)

    def test_le_chemin_de_parfile_ne_peut_pas_etre_injecte(self):
        """Le cas le plus proche du reel : un chemin de parfile.

        C'est l'argument que l'outil construit a partir du nom de
        schema, du repertoire DIRECTORY et du `run_id`. Une
        construction de schema malveillante qui passerait la validation
        ne doit pas pouvoir non plus transformer l'argument en commande.
        """
        chemin = "/tmp/a; touch %s; b" % self.temoin
        resultat = LocalRunner().run_script(
            build_script('osd_kv OSD_CHEMIN "$1"\nexit 0\n', [chemin]),
            timeout=60,
        )
        self.assertEqual(resultat.get("OSD_CHEMIN"), chemin)
        self.assertFalse(self.temoin.exists())


class TestEnvironnementDuScript(unittest.TestCase):
    """`build_script(env=...)` alimente le client Oracle distant."""

    def test_un_nom_invalide_est_refuse(self):
        """Le nom est pose dans le script **sans quoting**.

        Un nom contenant un espace, un `;` ou un chiffre initial
        cretrait soit une erreur de syntaxe, soit une commande. Le
        controle est ici plutot qu'en amont, parce que `env` est une
        surface d'appel interne qu'un adaptateur forget pourrait
        remplir a partir d'une valeur de configuration.
        """
        for nom in ("FOO-BAR", "1FOO", "FOO BAR", "FOO;rm", "FOO=$(id)",
                    "", "FOO\nBAR", "FOO'", 'FOO"', "FOO.BAR"):
            with self.subTest(nom=repr(nom)):
                with self.assertRaises(ValueError):
                    build_script("exit 0", ["x"], env={nom: "v"})

    def test_les_variables_de_contrat_du_shell_sont_refusees(self):
        """Eraser `PATH` ou `IFS` casse le script de facon opaque.

        Le symptome n'apparaitrait qu'a l'appel d'un binaire du
        prelude, sur l'hote de production, avec un message qui ne parle
        que de « command not found » — sans lien avec la cause.
        """
        for nom in sorted(runner._ENV_DENIED):
            with self.subTest(nom=nom):
                with self.assertRaises(ValueError) as ctx:
                    build_script("exit 0", ["x"], env={nom: "/tmp"})
                self.assertIn("protegee", str(ctx.exception))

    def test_un_nom_valide_est_accepte(self):
        script = build_script("exit 0", ["x"], env={"TNS_ADMIN": "/opt/net",
                                                    "ORACLE_SID": "OEMCC"})
        self.assertIn("TNS_ADMIN='/opt/net'", script)
        self.assertIn("export TNS_ADMIN", script)
        self.assertIn("ORACLE_SID='OEMCC'", script)

    def test_les_variables_sont_posees_avant_le_prelude(self):
        """Le prelude et le corps ont besoin de `TNS_ADMIN`.

        Une valeur posee apres le prelude n'aurait d'effet sur rien :
        le client Oracle est lance par le corps, et l'absence viendrait
        du client. C'est un echec qui ne se reproduit pas en developpement,
        ou l'environnement du developpeur est deja renseigne.
        """
        script = build_script("exit 0", ["x"], env={"TNS_ADMIN": "/opt/net"})
        self.assertLess(
            script.index("export TNS_ADMIN"),
            script.index("OSD_RESULT_BEGIN"),
        )

    def test_la_valeur_arrive_reellement_dans_le_shell(self):
        """Pas seulement « presente dans le texte » : **visible**.

        C'est la seule preuve qui compte, et elle attrape une erreur
        d'export oublie comme une erreur de quoting.
        """
        resultat = LocalRunner().run_script(
            build_script(
                'osd_kv OSD_TNS "$TNS_ADMIN"\n'
                'osd_kv OSD_SID "${ORACLE_SID-indefini}"\n'
                "exit 0\n",
                ["x"],
                env={"TNS_ADMIN": "/opt/net/admin", "ORACLE_SID": "OEMCC"},
            ),
            timeout=60,
        )
        self.assertEqual(resultat.get("OSD_TNS"), "/opt/net/admin")
        self.assertEqual(resultat.get("OSD_SID"), "OEMCC")

    def test_une_valeur_hostile_ne_petit_pas_injecter(self):
        for valeur in VALEURS_HOSTILES:
            if not valeur.strip():
                continue
            with self.subTest(valeur=repr(valeur)[:40]):
                resultat = LocalRunner().run_script(
                    build_script('printf %s "$TNS_ADMIN" >&3\nexit 0\n', ["x"],
                                 env={"TNS_ADMIN": valeur}),
                    timeout=60,
                )
                # Une valeur vide est un cas legitime : la variable n'est
                # simplement pas posee.
                attendu = "" if not valeur else valeur
                lignes = resultat.stdout_raw.splitlines()
                self.assertEqual(
                    "\n".join(lignes[1:-1]), attendu, repr(valeur)
                )

    def test_une_valeur_vide_n_expose_pas_de_variable(self):
        """Une valeur vide est omise, pas exportee a vide.

        Exporter `TNS_ADMIN=''` n'est pas neutre : le client Oracle
        interprets une chaine vide comme un chemin relatif au repertoire
        courant, et tente de charger `tnsnames.ora` depuis la ou il se
        trouve. L'echec serait `ORA-12154`, c'est-a-dire un message
        qui accuse le reseau alors que la cause est locale.
        """
        script = build_script("exit 0", ["x"], env={"TNS_ADMIN": ""})
        self.assertNotIn("TNS_ADMIN", script)

    def test_un_env_vide_ne_produit_aucune_ligne(self):
        """`env={}` et `env=None` doivent produire le meme script qu'absent.

        Sinon un chemin de code passant `env={}` produirait un script
        different, et les deux versions ne seraient testees qu'une seule.
        """
        sans = build_script("exit 0", ["x"])
        self.assertEqual(build_script("exit 0", ["x"], env={}), sans)
        self.assertEqual(build_script("exit 0", ["x"], env=None), sans)


class TestPortabiliteDesScripts(unittest.TestCase):
    """Les scripts distants partent sur AIX, ou `/bin/sh` est le Bourne shell.

Pas ksh93 : c'est le Bourne shell qui est en service, et il se distingue du
ksh93 sur deux points qui ne se voient pas à la relecture — `set -u` y rend
illicite toute expansion d'un paramètre non défini, et le trap de sortie n'y
reçoit pas le code de sortie. Ces deux points sont traités par le prelude et
couverts par des tests dédiés ; ce qui reste ici, c'est l'inventaire des
constructions que le Bourne shell refuse.
"""

    def setUp(self):
        self.dossier = SRC_DIR.parent / "shell"

    def _code(self, nom: str) -> str:
        """Le fichier **sans ses commentaires**, mis en forme ligne a ligne.

        Retirer les commentaires est indispensable : le prelude *documente*
        les constructions qu'il interdit, et un controle qui chercherait
        `eval` ou `grep -o` dans le texte declencherait l'alerte sur sa
        propre documentation.
        """
        source = (self.dossier / nom).read_text(encoding="utf-8")
        return "\n".join(
            ligne for ligne in source.splitlines()
            if not ligne.lstrip().startswith("#")
        )

    def test_aucune_construction_non_posix(self):
        """Le catalogue du prelude, applique mecaniquement a tous les scripts.

        Le prelude interdit lui-meme une liste de constructions ; rien
        n'empechait un script ajoute de s'en servir. Cette liste est
        donc un **contrat verifie**, pas une recommandation.

        Toutes les violations sont regroupees dans un seul message :
        en rapporter une a la fois ferait corriger, relancer, decouvrir
        la suivante — alors que la liste courte, et qu'un seul passage
        suffit.
        """
        interdits = {
            # `[[:espace:]]` est une classe de caracteres **POSIX**
            # dans une expression glob, pas l'operateur de test de ksh.
            # L'exclusion est donc necessaire, sans quoi le controle
            # accuse a tort le seul script qui utilise un glob.
            r"\[\[(?!:)": "test [[ ]] (ksh/bash)",
            r"(?<![\w-])local\s+\w": "local (non POSIX)",
            r"echo\s+-e\b": "echo -e (non POSIX)",
            r"printf\s+'?%q": "printf %q (bash)",
            r"<\(": "substitution de processus (bash)",
            r"(?<![\w-])read\s+-[a-zA-Z]*d": "read -d (bash)",
            r"\bgrep\b[^\n|;]*\s-o": "grep -o (non POSIX)",
            r"\bgrep\b[^\n|;]*\s-P\b": "grep -P (GNU)",
            r"\bsed\b[^\n|;]*\s-i(\s|$)": "sed -i (non POSIX)",
            r"\bcpio\b": "cpio (absent ou divergent sur AIX)",
            r"--login": "ssh --login",
            r"\bpython[0-9.]*\b": "python sur un hote AIX",
            r"\bperl\b": "perl sur un hote AIX",
        }
        violations = []
        for nom in CORPS:
            code = self._code(nom)
            for motif, raison in interdits.items():
                trouve = re.search(motif, code)
                if trouve:
                    numero = self._numero_de_ligne(code, trouve.start())
                    violations.append(
                        f"{nom}:{numero}: {raison} -> {trouve.group(0)!r}"
                    )
        self.assertEqual(violations, [], "\n".join(violations))

    def test_le_calcul_de_place_n_utilise_pas_eval(self):
        """Le script d'espace valide les chiffres de `df` sans `eval`.

        `eval "$osd_avail_kb + 1"` est le reflexe naturel, et il
        execute ce que `df` a imprime. Le controle doit donc etre un
        motif shell, ce qu'il est — mais la verification doit rester
        visible, parce que le reflexe revient.
        """
        code = self._code("remote_space.sh")
        self.assertIn("*[!0-9]*", code)
        self.assertNotRegex(code, r"(?<![\w-])eval(?![\w-])")

    @staticmethod
    def _numero_de_ligne(texte: str, position: int) -> int:
        return texte.count("\n", 0, position) + 1

    def test_aucun_eval_dans_un_seul_script(self):
        """Interdit par `AGENTS.md`, ecritement.

        `eval` sur une valeur d'argument est la seule construction qui
        transformerait une donnee en commande malgre un quoting
        correct. C'est le controle le plus important de ce module.

        La recherche ignore les commentaires — y compris ceux du
        prelude, qui *mentionnent* `eval` pour l'interdire. Un controle
        qui ne ferait pas cette distinction signalerait sa propre
        documentation et serait coupe net au premier coup d'oeil.
        """
        for nom in CORPS:
            with self.subTest(fichier=nom):
                self.assertNotRegex(self._code(nom),
                                    r"(?<![\w-])eval(?![\w-])")

    def test_le_shebang_annonce_bien_un_shell(self):
        """`remote_datapump.sh` annoncait `#!/usr/bin/env python3`.

        Le shebang est ici un commentaire — le script est envoye sur
        stdin — donc l'erreur etait invisible a l'execution. Elle ne
        le serait plus des que quelqu'un executerait le fichier
        directement pour le deboguer, et le message serait alors
        incomprehensible. Un shebang faux est un piege qui dort.
        """
        for nom in CORPS:
            source = (self.dossier / nom).read_text(encoding="utf-8")
            if not source.startswith("#!"):
                continue
            with self.subTest(fichier=nom):
                shebang = source.splitlines()[0]
                self.assertRegex(shebang, r"^#!/(usr/)?bin/sh\b")
                self.assertNotIn("python", shebang)

    def test_les_scripts_ne_contiennent_pas_de_chemin_de_developpement(self):
        """Un chemin absolu fige dans un script rendrait l'outil dependant
        du poste qui l'a ecrit.

        C'est le genre d erreur qui ne se voit qu'en exploitation,
        sur l'hote qui n'a pas le meme repertoire.
        """
        interdits = ("/data/docker", "/home/", "/Users/", "/tmp/opencode",
                     "/root/", "C:\\")
        for nom in CORPS:
            for chemin in interdits:
                with self.subTest(fichier=nom, chemin=chemin):
                    self.assertNotIn(chemin, (self.dossier / nom).read_text(
                        encoding="utf-8"))


class TestExtractionDesCodes(unittest.TestCase):
    """`osd_codes` remplace `grep -o`, qui n'est pas POSIX."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _extraire(self, texte: str, motif: str) -> str:
        """Applique `osd_codes` du prelude a un contenu donne.

        La fonction est **executee depuis le fichier**, et non recopiee
        dans le test : une recopie deriverait de la source et donnerait
        un faux succes des que la source change. Le `trap` est neutralise
        pour que le sourcing n'ouvre pas le canal machine.
        """
        chemin = Path(self.tmp.name) / "sortie"
        chemin.write_text(texte, encoding="utf-8")
        proc = subprocess.run(
            ["/bin/sh", "-c",
             '. "$0" >/dev/null 2>&1 || exit 1\n'
             'osd_codes "$1" < "$2"',
             str(SRC_DIR.parent / "shell" / "prelude.sh"),
             motif, str(chemin)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        return proc.stdout.decode("utf-8").strip()

    def test_les_codes_de_chaque_famille(self):
        """Data Pump parle en `ORA-`, `UDI-` et `DBMGSPC-`.

        Un seul petit `sed` n'aurait pas couvert le cas des trois
        familles sans alternance, et l'alternance POSIX n'existe pas en
        `sed` : c'est precisement pour cela que l'extraction est faite
        en `awk`.
        """
        sortie = self._extraire(
            "ORA-12545: cannot perform operation\n"
            "UDI-00008: operation failed\n"
            "DBMGSPC-01133: insufficient space\n",
            "(ORA|UDI|DBMGSPC)-[0-9]+",
        )
        self.assertEqual(set(sortie.split()), {"ORA-12545", "UDI-00008",
                                              "DBMGSPC-01133"})

    def test_deux_codes_sur_une_meme_ligne(self):
        """Un message Oracle en contient souvent deux, separes par un blanc."""
        sortie = self._extraire(
            "ORA-01017: invalid credentials caused ORA-12541 to fail\n",
            "ORA-[0-9][0-9]*",
        )
        self.assertEqual(set(sortie.split()), {"ORA-01017", "ORA-12541"})

    def test_un_code_colle_a_du_texte_est_reconnu(self):
        """`ORA-01017:invalid` n'a pas de blanc apres les deux-points."""
        self.assertEqual(self._extraire("ORA-01017:x\n", "ORA-[0-9][0-9]*"),
                         "ORA-01017")

    def test_un_journal_reussi_ne_rend_rien(self):
        """Un faux positif ici ferait echouer un run parfaitement reussi.

        `expdp` ecrit `Job: SYS_EXPORT_TABLE_01 completed successfully`, ce
        qui ne contient aucun code : la fonction doit rendre une chaine
        vide, et non un motif approximatif.
        """
        self.assertEqual(
            self._extraire(
                "Job: SYS_EXPORT_TABLE_01\n"
                "SYS_EXPORT_TABLE_01: HR 68 objects\n"
                "completed successfully\n",
                "(ORA|UDI|DBMGSPC)-[0-9]+",
            ),
            "",
        )

    def test_les_doublons_sont_elimines(self):
        sortie = self._extraire("ORA-39000\nORA-39000\nORA-39002\n",
                                "ORA-[0-9][0-9]*")
        self.assertEqual(sortie.split(), ["ORA-39000", "ORA-39002"])

    def test_une_entree_vide_ne_produit_rien(self):
        """Pas de code en erreur de segmentation sur un journal vide.

        Un journal vide est normal : un `expdp` echoue immediatement
        (reseau, privilege) sans avoir ecrit une ligne. La fonction
        doit rendre une chaine vide et le script doit poursuivre.
        """
        self.assertEqual(self._extraire("", "ORA-[0-9][0-9]*"), "")

    def test_une_ligne_sans_blanc_n_est_pas_decoupee_sauvagement(self):
        """Les separateurs Oracle sont des blancs, pas des deux-points.

        Une extraction trop gloutonne a la recherche de motifs
        intermediaires découperait `ORA-01017:invalid:credentials` en
        morceaux et pourrait en fabriquer un qui ressemble a un code.
        """
        sortie = self._extraire("ORA-39000:foo-bar-baz-12345\n",
                                "(ORA|UDI|DBMGSPC)-[0-9]+")
        self.assertEqual(sortie, "ORA-39000")


class TestAnalyseDuBlocDeResultat(unittest.TestCase):
    def _bloc(self, corps: str, rc: int = 0) -> str:
        return (f"OSD_RESULT_BEGIN\n{corps}\nOSD_RESULT_END rc={rc}\n")

    def test_un_bloc_complet_est_analyse(self):
        r = runner._parse_result(
            self._bloc("OSD_RC=0\nCLE=valeur"), "", 0, command="x"
        )
        self.assertEqual(r.get("CLE"), "valeur")
        self.assertEqual(r.get_int("OSD_RC"), 0)
        self.assertTrue(r.ok)

    def test_les_lignes_de_resultat_sont_separees_des_donnees(self):
        r = runner._parse_result(
            self._bloc("OSD_ROWS_BEGIN\nligne 1\nligne 2\nOSD_ROWS_END"),
            "", 0, command="x",
        )
        self.assertEqual(r.rows, ["ligne 1", "ligne 2"])
        self.assertEqual(r.get("ligne 1", ""), "", "une ligne ne doit pas "
                                                        "devenir une cle")

    def test_une_ligne_sans_egal_ne_perturbe_pas_l_analyse(self):
        """Une sortie parasite ne doit pas faire echouer la lecture.

        Le contrat dit que stdout ne contient que le bloc, mais un
        `.bashrc` distant qui bavarde, ou un `ssh` qui emet un avertissement
        sur stdout, ajouteraient des lignes. Les perdre dans
        `stdout_raw` est preferable a perdre le resultat.
        """
        r = runner._parse_result(
            self._bloc("Welcome to AIX\nOSD_RC=0"), "", 0, command="x"
        )
        self.assertEqual(r.get_int("OSD_RC"), 0)
        self.assertIn("Welcome to AIX", r.stdout_raw)

    def test_un_bloc_incomplet_est_une_erreur_explicite(self):
        """Un bloc sans `END` signifie que le script est mort en route.

        Rendre un `Result` partiel ferait croire a un echec metier
        alors que la cause est le protocole : c'est la distinction que
        l'exploitant doit pouvoir faire avant de lever une alerte.
        """
        for sortie in (
            "",
            "OSD_RESULT_BEGIN\nOSD_RC=0\n",
            "OSD_RC=0\nOSD_RESULT_END rc=0\n",
            "OSD_RESULT_BEGIN\n",
        ):
            with self.subTest(sortie=repr(sortie)[:40]):
                with self.assertRaises(RemoteProtocolError) as ctx:
                    runner._parse_result(sortie, "", 1, command="x")
                self.assertEqual(ctx.exception.code, ec.PREREQ)

    def test_le_bloc_incomplet_explique_les_causes_probables(self):
        """Un message d erreur doit proposer la suite a examiner.

        Les deux causes reelles sont le shell distant absent et la
        session SSH fermee par `BatchMode` ; les nommer evite un aller
        retour de support.
        """
        with self.assertRaises(RemoteProtocolError) as ctx:
            runner._parse_result("", "sh: not found", 127, command="x")
        self.assertIn("sh -s", ctx.exception.hint)
        self.assertIn("BatchMode", ctx.exception.hint)

    def test_le_code_du_bloc_prime_sur_le_code_du_processus(self):
        """`exit 3` puis le `trap` : le processus rend 3, mais le `trap`
        peut avoir enregistre autre chose.

        La valeur du marqueur `OSD_RESULT_END rc=` est la vue du script
        sur lui-meme, donc elle fait foi : c'est elle qui distingue
        « le script s'est termine normalement » de « le script a ete
        tue par un signal, et le trap a rendu 0 ».
        """
        r = runner._parse_result(self._bloc("OSD_RC=3", rc=3), "", 137,
                                 command="x")
        self.assertEqual(r.rc, 3)

    def test_un_marqueur_sans_rc_conserve_le_code_du_processus(self):
        r = runner._parse_result(
            self._bloc("OSD_RC=0").replace(" rc=0\n", "\n"), "", 5, command="x"
        )
        self.assertEqual(r.rc, 5)

    def test_le_fatal_est_recupere_separement(self):
        """`OSD_FATAL` est un diagnostic, pas une donnee exploitable.

        Le mettre dans `kv` le ferait apparaitre comme une valeur
        ordinaire dans le rapport et dans l'etat, ou il serait lu
        comme un resultat metier.
        """
        r = runner._parse_result(
            self._bloc("OSD_FATAL=binaire absent du PATH: expdp", rc=127),
            "", 127, command="x",
        )
        self.assertEqual(r.kv["__fatal__"], "binaire absent du PATH: expdp")

    def test_les_clefs_ora_sont_reconnues_au_meme_titre_que_les_autres(self):
        """Une reponse utile peut porter le prefixe `OSD_`.

        Filtrer toutes les lignes `OSD_*` — ce qui serait l'intuition
        naturelle — ferait perdre `OSD_AVAIL_BYTES` et
        `OSD_ORACLE_CODES`, c'est-a-dire l'espace disque et les codes
        d erreur.
        """
        r = runner._parse_result(
            self._bloc("OSD_AVAIL_BYTES=1234\nOSD_FOUND=1"), "", 0, command="x"
        )
        self.assertEqual(r.get_int("OSD_AVAIL_BYTES"), 1234)
        self.assertEqual(r.get("OSD_FOUND"), "1")

    def test_get_int_ne_leve_pas_sobre_valeur_illisible(self):
        r = runner._parse_result(self._bloc("N=pas_un_nombre"), "", 0, command="x")
        self.assertEqual(r.get_int("N", -1), -1)
        self.assertEqual(r.get_int("ABSENT", 42), 42)

    def test_le_dictionnaire_rapporte_est_redacte(self):
        """`as_safe_dict` alimente l'etat et le rapport JSON.

        Sans redaction, une valeur contenant un mot de passe — un
        message Oracle de connexion, une URL de wallet — se retrouverait
        dans un fichier lisible par tous les comptes du serveur de
        saut.
        """
        r = runner._parse_result(
            self._bloc("DETAIL=hote scott/tiger@db.net"), "", 0, command="x"
        )
        self.assertNotIn("tiger", str(r.as_safe_dict()))

    def test_les_codes_ora_sont_collectes_partout(self):
        r = runner.Result(
            rc=1,
            kv={"OSD_ORACLE_ERROR": "1", "OSD_ORACLE_CODES": "ORA-12545 ORA-12541"},
            stdout_raw="journal: ORA-39000 vu",
            stderr="et ORA-12545 sur stderr",
        )
        self.assertEqual(runner.oracle_error_codes(r),
                         ["ORA-12541", "ORA-12545", "ORA-39000"])

    def test_aucun_code_lorsque_le_bloc_est_propre(self):
        r = runner.Result(rc=0, stdout_raw="completed successfully", stderr="")
        self.assertEqual(runner.oracle_error_codes(r), [])

    def test_une_entree_traduite_ne_cache_pas_le_code(self):
        """La detection ne doit dependre d'aucun texte traduit.

        Un message en allemand ou en japonais contient toujours un
        `ORA-xxxxx` a l'identique : c'est la seule partie stable, et
        c'est sur elle que repose la classification.
        """
        r = runner.Result(rc=1, stdout_raw="ORA-01950: keine Berechtigung")
        self.assertEqual(runner.oracle_error_codes(r), ["ORA-01950"])


class TestLigneDeCommandeSsh(unittest.TestCase):
    def test_un_hote_est_obligatoire(self):
        """Sans hote, il n'y a pas d'execution distante a decrire.

        Levyer ici plutot que de laisser un `ssh ''` produire un
        diagnostic incomprehensible sur l'hote de production.
        """
        with self.assertRaises(PrereqError):
            RemoteRunner(host="")

    def test_la_ligne_ne_contient_que_sh_et_s(self):
        """`sh -s` : le script arrive par stdin, rien ne transite par argv.

        C'est ce qui garantit qu'un SQL ou une chaine de connexion
        n'apparait pas dans `ps` sur l'hote, ni dans la liste des
        processus visible par les autres comptes.
        """
        argv = RemoteRunner(host="hote-aix", user="oracle").argv()
        self.assertEqual(argv[-2:], ["sh", "-s"])

    def test_le_cible_est_le_bon_cote(self):
        self.assertEqual(RemoteRunner(host="h", user="u").target, "u@h")
        self.assertEqual(RemoteRunner(host="h").target, "h")

    def test_les_options_ssh_sont_transmises(self):
        argv = RemoteRunner(
            host="h", ssh_opts=["BatchMode=yes", "ConnectTimeout=15", "  "]
        ).argv()
        # L'option vide est ignoree : elle produirait un `-o ""` que
        # certaines versions de ssh refusent.
        self.assertEqual(argv.count("-o"), 2)
        self.assertIn("BatchMode=yes", argv)

    def test_batchmode_est_impose_et_non_repris_tel_quel(self):
        """`BatchMode=no` est ecarte, pas transmis.

        C'est le mode d'echec le plus cher du projet : `ssh` ouvre une
        invite de mot de passe que rien ne repondra sous cron, et le run
        reste bloque jusqu'a l'expiration du crontab — sans journal, sans
        code de sortie, et avec un `crontab` que l'exploitant ne remarque
        pas.

        La validation de configuration refuse deja cette valeur, mais
        cette classe est construite directement par les tests et par
        tout appelant futur. Elle ne peut donc pas faire confiance a une
        validation amont qu'elle ne voit pas passer, et le test fixe la
        garantie **ici**, au seul endroit ou elle est verifiable sans
        monter une configuration complete.
        """
        for valeur in ("no", "NO", "askpass", "oui"):
            with self.subTest(valeur=valeur):
                argv = RemoteRunner(
                    host="h", ssh_opts=[f"BatchMode={valeur}", "ConnectTimeout=5"]
                ).argv()
                occurrences = [a for a in argv if a.lower().startswith("batchmode")]
                self.assertEqual(occurrences, ["BatchMode=yes"])
                # Les autres options survivent : ecarter `BatchMode` ne
                # doit pas vider la liste.
                self.assertIn("ConnectTimeout=5", argv)

    def test_la_cle_d_authentification_est_pas_un_secret(self):
        """`-i` expose un chemin, jamais la cle elle-meme.

        Le chemin peut figurer dans les journaux ; la cle, non. C'est
        pourquoi le mode du fichier est verifie a la configuration et
        que la cle n'est jamais lue par l'outil.
        """
        cle = "/home/osd/.ssh/id_rsa"
        argv = RemoteRunner(host="h", identity=cle).argv()
        self.assertIn("-i", argv)
        self.assertIn(cle, argv)
        self.assertNotIn("BEGIN", " ".join(argv))

    def test_le_multiplexage_est_ajoute_sans_doublon(self):
        argv = RemoteRunner(
            host="h", control_path="/tmp/ctl-%r@%h:%p"
        ).argv()
        self.assertIn("ControlPath=/tmp/ctl-%r@%h:%p", argv)
        self.assertIn("ControlMaster=auto", argv)

    def test_le_libelle_designe_le_cote_et_n_expose_rien_dautre(self):
        """Le libelle finit dans le rapport et dans les messages d erreur.

        Il doit contenir le compte et l'hote, qui ne sont pas des
        secrets, et rien d'autre — en particulier aucune chaine de
        connexion, qui porterait le `userid`.
        """
        r = RemoteRunner(host="ora-aix01", user="oracle")
        self.assertEqual(r.label, "ssh:oracle@ora-aix01")
        for motif in ("L_SRC", "userid", "tiger", "wallet"):
            with self.subTest(motif=motif):
                self.assertNotIn(motif, r.label)
        self.assertIn("localhost", LocalRunner().label)


class TestExecutionLocaleDeReference(unittest.TestCase):
    """Le protocole complet, execute pour de vrai, sans base ni reseau.

    C'est le test le plus utile du module : il eprouve la chaine
    `build_script` -> `/bin/sh -s` -> `trap` -> bloc machine ->
    `_parse_result` avec les **vrais** scripts de `shell/`. Une
    regression du prelude (une redirection oubliee, un `trap` mal
    place) le fait echouer ici, sans qu'aucune base ne soit allumee.
    """

    def test_un_binaire_present_est_signale_present(self):
        resultat = LocalRunner().run_script(
            build_script(load_body("remote_which.sh"), ["sh"]), timeout=60
        )
        self.assertTrue(resultat.ok, resultat.stderr)
        self.assertEqual(resultat.get("OSD_FOUND"), "1")
        self.assertTrue(resultat.get("OSD_PATH").endswith("/sh"))

    def test_un_binaire_absent_est_signale_absent(self):
        """Le code 127 seul serait ambigu : il peut aussi dire « erreur
        interne du shell ». C'est `OSD_FOUND` qui tranche, et c'est lui
        que l'etape 3 des dependances lit.
        """
        resultat = LocalRunner().run_script(
            build_script(load_body("remote_which.sh"),
                         ["binaire_osd_qui_nexiste_pas"]), timeout=60
        )
        self.assertEqual(resultat.rc, 127)
        self.assertEqual(resultat.get("OSD_FOUND"), "0")
        self.assertIn("__fatal__", resultat.kv)

    def test_une_commande_absente_nomine_ce_qui_manque(self):
        resultat = LocalRunner().run_script(
            build_script(load_body("remote_exec.sh"),
                         ["binaire_osd_qui_nexiste_pas"]), timeout=60
        )
        self.assertEqual(resultat.get("OSD_MISSING_CMD"),
                         "binaire_osd_qui_nexiste_pas")

    def test_la_sortie_dune_commande_est_placee_dans_les_lignes(self):
        resultat = LocalRunner().run_script(
            build_script(load_body("remote_exec.sh"), ["echo", "bonjour"]),
            timeout=60,
        )
        self.assertEqual(resultat.rows, ["bonjour"])

    def test_les_arguments_dune_commande_sont_preserves(self):
        """Un chemin de dump ou un nom de fichier a espaces doit passer.

        C'est le chemin du `ls` de l'etape 12 : un `set --` mal construit
        ouferait le chemin en deux mots et listerait le mauvais
        repertoire, sans erreur visible.
        """
        resultat = LocalRunner().run_script(
            build_script(load_body("remote_exec.sh"),
                         ["printf", "%s\\n", "un seul argument avec espaces"]),
            timeout=60,
        )
        self.assertEqual(resultat.rows, ["un seul argument avec espaces"])

    def test_la_sortie_erreur_va_dans_stderr_et_pas_dans_le_bloc(self):
        """stdout ne porte que le bloc machine : c'est ce qui rend
        l'analyse deterministe.

        Un message d erreur dans le bloc se retrouverait dans `rows`,
        donc dans l'inventaire d'objets de l'etape 16, et ferait
        compter une ligne de diagnostic comme un objet du schema.
        """
        resultat = LocalRunner().run_script(
            build_script(load_body("remote_exec.sh"),
                         ["sh", "-c", "echo sortie; echo erreur >&2"]),
            timeout=60,
        )
        self.assertEqual(resultat.rows, ["sortie"])
        self.assertIn("erreur", resultat.stderr)
        self.assertIn("[remote]", resultat.stderr)

    def test_espace_disque_reel(self):
        """`remote_space.sh` sur le `TMPDIR` reel, sans mock.

        Le script est celui de l'etape 9. Le control verifie que les
        chiffres sont coherents entre eux, ce qui attraperait une
        colonne de `df` lue a la mauvaise position — un defaut qui ne
        se voit que sur un `df` a six colonnes differentes de celui du
        poste de developpement.
        """
        resultat = LocalRunner().run_script(
            build_script(load_body("remote_space.sh"), [tempfile.gettempdir()]),
            timeout=60,
        )
        self.assertTrue(resultat.ok, resultat.stderr)
        self.assertEqual(resultat.get("OSD_EXISTS"), "1")
        total = resultat.get_int("OSD_TOTAL_BYTES")
        libre = resultat.get_int("OSD_AVAIL_BYTES")
        self.assertGreater(total, 0)
        self.assertGreater(libre, 0)
        self.assertLessEqual(libre, total)
        self.assertEqual(total, resultat.get_int("OSD_TOTAL_KB") * 1024)

    def test_un_chemin_absent_est_signale_absent(self):
        resultat = LocalRunner().run_script(
            build_script(load_body("remote_space.sh"),
                         ["/nonexistent/osd/xyz"]), timeout=60
        )
        self.assertEqual(resultat.rc, 66)
        self.assertEqual(resultat.get("OSD_EXISTS"), "0")

    def test_la_sortie_de_travail_ne_pollue_pas_stdout(self):
        """Meme un script bavard produit-il un bloc analysable.

        Un `echo` oublie dans le corps part vers stderr grace au
        `exec 1>&2` du prelude. Sans lui, ce texte atterrirait dans
        stdout, entre le `BEGIN` et l'`END`, et la ligne serait lue
        comme une donnee cle/valeur.
        """
        resultat = LocalRunner().run_script(
            build_script(
                'echo "bavardage sans controle"\n'
                'echo "sur stderr" >&2\n'
                'osd_kv OSD_CLE 42\n'
                "exit 0\n",
                ["x"],
            ),
            timeout=60,
        )
        self.assertEqual(resultat.get("OSD_CLE"), "42")
        self.assertIn("bavardage sans controle", resultat.stderr)
        self.assertNotIn("bavardage", resultat.rows)
        self.assertEqual(len(resultat.rows), 0)

    def test_une_sortie_sans_saut_de_ligne_final_ne_casse_pas_le_bloc(self):
        """Le piege que le prelude corrige, et qu'il ne faut pas rejouer.

        Un corps dont la sortie ne se termine pas par un saut de ligne
        collait sa donnee au marqueur de fin :

            OSD_RESULT_BEGIN
            ORA-01017:expiredOSD_RESULT_END rc=0

        Le bloc devenait alors illisible, et l'echec etait attribue au
        protocole — « bloc de resultat distant incomplet » — alors que
        l'export avait parfaitement reussi. C'est le genre d'erreur qui
        n'apparait qu'en production, sur un `cat` d'un fichier dont la
        derniere ligne n'est pas terminee.
        """
        resultat = LocalRunner().run_script(
            build_script('printf %s "$1" >&3\nexit 0\n', ["sans-accent-final"]),
            timeout=60,
        )
        self.assertTrue(resultat.ok)
        lignes = resultat.stdout_raw.splitlines()
        self.assertIn("OSD_RESULT_BEGIN", lignes)
        self.assertIn("OSD_RESULT_END rc=0", lignes)
        self.assertIn("sans-accent-final", lignes)

    def test_une_donnee_pouvant_ressembler_au_marqueur_ne_le_tronque_pas(self):
        """L'ancrage du marqueur doit proteger la charge utile.

        Une valeur contenant `OSD_RESULT_END` au milieu d'un texte ne
        doit pas etre prise pour la fin du bloc : sinon un schema
        portant ce nom, ou un message Oracle l'ayant recense,
        tronquerait le resultat en silence, et les controles en aval
        working sur des donnees absentes repondraient « OK » par absence.
        """
        resultat = LocalRunner().run_script(
            build_script(
                'osd_kv OSD_TRAPPIE "avantOSD_RESULT_END rc=0apres"\nexit 0\n',
                ["x"],
            ),
            timeout=60,
        )
        self.assertEqual(resultat.get("OSD_TRAPPIE"),
                         "avantOSD_RESULT_END rc=0apres")
        self.assertEqual(resultat.rc, 0)

    def test_un_code_de_sortie_non_nul_est_conserve(self):
        """Le code doit ressortir par `osd_exit`, pas par un `exit` nu.

        Un `exit 7` ecrit directement dans le corps est tolere (le trap
        le voit sur les shells POSIX corrects) mais n'est plus la forme
        attendue : c'est `osd_exit 7` qui garantit le code sur tous les
        shells, Bourne d'AIX compris. Le test porte donc sur `osd_exit`,
        et verifie en prime que le `rc` du bloc vaut bien 7.
        """
        resultat = LocalRunner().run_script(
            build_script("osd_kv OSD_X 1\nosd_exit 7\n", ["x"]), timeout=60
        )
        self.assertEqual(resultat.rc, 7)
        self.assertFalse(resultat.ok)

    def test_un_delai_depasse_donne_un_prerequis_et_non_un_bloc_vide(self):
        """L'attente sans borne est le pire des echecs sous cron.

        L'execution resterait bloquee jusqu'a l'arret de l'ordonnanceur,
        sans journal et sans code de sortie. Le delai transforme
        l'attente en echec explicite, avec le stderr de ce qui tournait
        encore.
        """
        with self.assertRaises(PrereqError) as ctx:
            LocalRunner().run_script(
                build_script("sleep 30\nexit 0\n", ["x"]), timeout=2
            )
        self.assertEqual(ctx.exception.code, ec.PREREQ)
        self.assertIn("delai", ctx.exception.message)

    def test_le_rapport_ne_contient_aucun_secret(self):
        """Le bloc de resultat finit dans l'etat et le rapport JSON.

        Un mot de passe de parfile passe par le canal `bootstrap`, donc
        dans le script ; le rapport ne doit pas le reconduire.
        """
        resultat = LocalRunner().run_script(
            build_script(
                'osd_kv OSD_USERID "scott/tiger@L_SRC"\nexit 0\n',
                ["x"],
            ),
            timeout=60,
        )
        self.assertIn("tiger", resultat.get("OSD_USERID"))  # le shell voit tout
        self.assertNotIn("tiger", str(resultat.as_safe_dict()))


class TestJournaux(unittest.TestCase):
    def test_la_commande_journalisee_ne_contient_pas_de_secret(self):
        cite = runner.quote_for_log(
            ["expdp", "userid=scott/tiger@L_SRC", "schemas=HR"]
        )
        self.assertNotIn("tiger", cite)
        self.assertIn("expdp", cite)

    def test_la_citation_est_rejouable(self):
        """Le journal doit permettre de recopier la commande telle quelle.

        C'est ce qui rend un incident reproductible : si la citation est
        approximative, l'exploitant reproduit autre chose que ce qui a
        echoue.
        """
        cite = runner.quote_for_log(["ls", "-l", "/chemin/avec espace"])
        # `set --` + `"$@"` : c'est ainsi que la citation est rejouee,
        # argument par argument, comme le ferait un operateur.
        proc = subprocess.run(
            ["/bin/sh", "-c", f'set -- {cite}; printf "%s|" "$@"'],
            stdout=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(
            proc.stdout.decode(), "ls|-l|/chemin/avec espace|"
        )


if __name__ == "__main__":
    unittest.main()
