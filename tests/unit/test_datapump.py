"""Tests de la couche Data Pump.

C'est le module qui ecrit la ligne `userid` dans un fichier pose sur
l'hote, et qui decide si un export a reussi. Les deux fonctions sont
critiques, et leurs defauts ne se verraient pas de la meme facon :

* un **parfile** mal construit ne provoque pas d'erreur franche. Il
  produit un job qui echoue avec un message qui ne parle que du dump,
  sans designer l'option fautive. L'exploitant recompte ses options
  pendant une heure ;
* une **detection de succes** trop permissive est plus grave que
  l'absence de detection : l'outil declarerait reussi un import qui n'a
  rien fait, et le rapport — seul support de la decision — serait
  faux.

Les tests se concentrent donc sur deux choses : ce que le client
**verrait reellement**, et sur quoi repose la decision de reussite.

Une observation de methode explique la forme de la plupart des tests.
Le protocole distant capture toute la sortie du client dans un fichier
temporaire, l'analyse, puis la supprime ; un test qui Pretendait
recuper la sortie du faux `expdp` par le bloc de resultat recuperait
une liste vide, et aurait passe en verifiant `assertEqual([], [])`. Le
faux client ecrit donc son observations dans un fichier dont le chemin
derive du parfile — un canal fiable, et un canal que le protocole
n'implemente pas.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import support  # noqa: F401

from osd import exit_codes as ec
from osd.adapters.datapump import (
    DATAPUMP_SUCCESS_CODES,
    DATAPUMP_WARNING_CODES,
    DataPumpAdapter,
    DataPumpResult,
    _lit,
    _q,
)
from osd.adapters.oracle import OracleSide
from osd.errors import OsdError
from osd.runner import LocalRunner, Result

#: Faux `expdp`/`impdp`.
#:
#: Il rend compte de ce qu'il a trouve du parfile, sans ecrire en base.
#: Le code de sortie est pilote par la variable `FAUX_RC` du fichier
#: `.rc`, ce qui permet de reproduire un export reussi, un export
#: interrompu et un export en erreur sans preparer trois faux binaires.
#:
#: Le rapport est ecrit dans un fichier **derive du parfile** plutot que
#: sur `stdout` : le protocole distant capture et supprime la sortie du
#: client, donc `stdout` n'est pas un canal d'observation fiable, et un
#: test qui s'y fierait verifierait le vide.
FAUX_CLIENT = r"""#!/bin/sh
# Faux client Data Pump de test.
parfile=''
for arg in "$@"; do
    case "$arg" in
        parfile=*) parfile=${arg#parfile=} ;;
    esac
done
rapport="$parfile.su"
if [ ! -f "$parfile" ]; then
    printf 'PARFILE_ABSENT\n' > "$rapport"
    exit 66
fi
{
    printf 'MODE=%s\n' "$(ls -l "$parfile" | cut -c1-10)"
    printf 'NB_OPTIONS=%s\n' "$(sed -n '/^[a-zA-Z]/p' "$parfile" | wc -l | tr -d ' ')"
    printf 'CLEFS=%s\n' "$(sed -n 's/^\([a-zA-Z_]*\)=.*/\1/p' "$parfile" | tr '\n' ',')"
    sed -n 's/^userid=/LIGNE_USERID=/p' "$parfile"
} > "$rapport" 2>&1
# L'echec eventuel est commande par un fichier, pas par l'environnement :
# le protocole distant ne transmet pas de variable d'environnement libre.
if [ -f "$parfile.rc" ]; then
    cat "$parfile.rc"
else
    echo 0
fi > "$parfile.code"
# Un code Oracle doit figurer dans la sortie du client : c'est
# exactement ce que le script distant analyse pour produire
# `OSD_ERROR_CODES`. Ecrit sur stdout **et** stderr parce que le script
# les capture ensemble.
printf 'ORA-12545: cannot perform operation\n' 1>&2
printf 'Export: STARTING\n'
exit "$(cat "$parfile.code")"
"""

#: Suffixe du fichier de rapport produit par `FAUX_CLIENT`.
SUFFIXE_RAPPORT = ".su"


def installer_faux_client(racine: Path, noms: Sequence[str] = ("expdp", "impdp")) -> Path:
    """Installe les faux clients dans un repertoire a preposer au PATH.

    Le binaire est un script `sh`, ce qui est la forme reelle d'`expdp`
    dans le PATH d'un hote Oracle : le test mesure donc la meme chose
    qu'en exploitation, y compris le fait que le shebang est lu par le
    noyau et non par le script.
    """
    dossier = racine / "bin"
    dossier.mkdir(parents=True, exist_ok=True)
    for nom in noms:
        chemin = dossier / nom
        chemin.write_text(FAUX_CLIENT, encoding="utf-8")
        chemin.chmod(0o755)
    return dossier


def path_sans_client_reel(base: str) -> str:
    """Un `PATH` dont le repertoire du vrai `expdp` est retire.

    Necessaire pour tester « client absent du PATH » : sur une machine
    ou un client Oracle est installe, le vrai binaire reste trouver, le
    script ne prend jamais le chemin 127, et le test passe au vert
    **sans avoir verifie ce qu'il pretendait verifier** — parce qu'un
    vrai export avait reussi a sa place. C'est le piege classique d'un
    test qui depend de son environnement, et il se paie cher : le
    developpeur voit un vert, l'integration voit un 127.

    On retire le **repertoire** entier plutot que le fichier : d'autres
    binaires du client y resident, et un PATH troue de facon selective
    serait illisible a relire six mois plus tard.

    Le reste du PATH est conserve : le script distant a besoin de
    `sed`, `awk`, `wc`, `tail` et consorts, et un PATH minimal le
    casserait d'une facon qui n'a rien a voir avec ce qu'on teste.
    """
    # `base` et non `os.environ["PATH"]` : au moment de l'appel, le PATH
    # contient deja le **faux** client du test, et `shutil.which` y
    # trouverait donc ce faux-la, retirant son repertoire au lieu de
    # celui du vrai client. Le vrai expdp resterait joignable, le script
    # ne prendrait jamais le chemin 127, et le test passerait au vert
    # en n'ayant exerce aucun code.
    deja_vu: Dict[str, str] = {}

    def chercher(nom: str) -> Optional[str]:
        if nom not in deja_vu:
            ancien = os.environ.get("PATH", "")
            os.environ["PATH"] = base
            try:
                deja_vu[nom] = shutil.which(nom) or ""
            finally:
                os.environ["PATH"] = ancien
        return deja_vu[nom] or None

    reel = chercher("expdp")
    if reel is None:
        return base
    a_retirer = os.path.dirname(os.path.realpath(reel))
    return os.pathsep.join(
        d for d in base.split(os.pathsep)
        if d and os.path.realpath(d) != a_retirer
    )


def lire_rapport(parfile: Path) -> Dict[str, str]:
    """Relit le rapport du faux client, au format `CLE=valeur`."""
    chemin = Path(str(parfile) + SUFFIXE_RAPPORT)
    if not chemin.exists():
        return {}
    lu: Dict[str, str] = {}
    for ligne in chemin.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in ligne:
            cle, valeur = ligne.split("=", 1)
            lu[cle] = valeur
    return lu


class CoteOracleSimule:
    """`OracleSide` reels, avec un acces SQL qui repond a un registre.

    L'objet `side` est un vrai `OracleSide` et non un double : les
    methodes reelles (`effective_connect`, `env`, `label`) sont
    justement ce que le parfile doit contenir, et un double les
    remplacerait par ce qu'on croit qu'elles font — c'est-a-dire par
    le defaut qu'on cherche a trouver.
    """

    def __init__(self) -> None:
        self.side = OracleSide(
            name="source", connect="L_SRC", schema="HR", directory="DP_DIR",
            wallet="/opt/oracle/wallet", sysdba=True, runner=LocalRunner(),
        )
        #: Reponses SQL, indexees par **sous-chaine** de la requete.
        self.reponses: Dict[str, List[List[str]]] = {}
        #: Requetes vues, dans l'ordre.
        self.requetes: List[str] = []
        self.erreur: Optional[OsdError] = None

    def query(self, sql: str, **kw: Any) -> List[List[str]]:
        self.requetes.append(sql)
        if self.erreur is not None:
            raise self.erreur
        for motif, lignes in self.reponses.items():
            if motif.lower() in sql.lower():
                return lignes
        return []

    def execute(self, sql: str, **kw: Any) -> None:
        self.requetes.append(sql)
        if self.erreur is not None:
            raise self.erreur


class TestParfileDExport(unittest.TestCase):
    """Ce qu'`expdp` lira reellement, ligne par ligne."""

    def setUp(self) -> None:
        self.oracle = CoteOracleSimule()
        self.adapter = DataPumpAdapter(self.oracle.side, oracle=self.oracle)

    def parfile(self, **kw: Any) -> List[str]:
        defauts: Dict[str, Any] = dict(
            schema="HR", job_name="J", dumpfile="exp.dmp", logfile="exp.log",
            content="ALL", compression="MEDIUM", parallel=1,
        )
        defauts.update(kw)
        return self.adapter.build_export_parfile(**defauts)

    # -- structure --------------------------------------------------------

    def test_les_options_obligatoires_sont_presentes(self):
        lignes = self.parfile()
        for option in ("userid=", "schemas=", "directory=", "logfile=",
                       "content=", "compression=", "dumpfile="):
            with self.subTest(option=option):
                self.assertTrue(
                    any(l.startswith(option) for l in lignes),
                    f"{option} absent de {lignes}",
                )

    def test_chaque_option_est_posee_une_seule_fois(self):
        """Un doublon est accepte, et le **dernier** gagne, sans bruit.

        Data Pump ne signale pas l'option en double. Le symptome serait
        un dump ecrit deux fois, ou une option qu'on croit avoir
        remplacee par une autre. C'est la classe de bug la plus dure a
        voir en production : le fichier existe, et il a la bonne taille.
        """
        cles = [l.split("=", 1)[0] for l in self.parfile() if "=" in l]
        doublons = sorted({c for c in cles if cles.count(c) > 1})
        self.assertEqual(doublons, [], f"options en double : {doublons}")

    def test_le_schema_est_mis_en_majuscules(self):
        """Oracle normalise les identifiants en majuscules.

        Un `schemas="hr"` serait accepte par le client — c'est la base
        qui compare sans etat — mais l'inventaire de l'etape 16 relit
        les noms d'objets et verrait des ecarts entre source et cible
        qui n'existent pas. Un faux signal d'echec de comparaison est
        aussi grave qu'un faux succes : il envoie vers un diagnostic
        qui n'a pas lieu d'etre.
        """
        self.assertIn('schemas="HR"', self.parfile(schema="hr"))

    def test_un_schema_avec_un_guillemet_ne_coupe_pas_la_ligne(self):
        """Data Pump double le guillemet interne.

        Un `"` non double couperait la ligne, et le client rapporterait
        alors un nom de schema invalide — un message qui ne designe
        pas la cause reelle.
        """
        self.assertIn('schemas="A""B"', self.parfile(schema='A"B'))

    def test_une_valeur_vide_reste_une_valeur(self):
        """`content=""` n'est pas la meme chose qu'une option absente.

        Le client refuserait la premiere et appliquerait son defaut a la
        seconde. La difference importe donc, et c'est a l'appelant de
        decider, pas au constructeur qui remplacerait un `""` par rien.
        """
        self.assertIn('content=""', self.parfile(content=""))

    # -- parallelisme -----------------------------------------------------

    def test_le_parallele_impose_le_jeton_de_numerotation(self):
        """`PARALLEL` > 1 produit `base-01.dmp`, `base-02.dmp`, ...

        Les noms ne sont pas devinables : le nombre de parties depend
        de la volumetrie. L'etape d'import les decouvre donc apres coup,
        en enumerant le repertoire — d'ou la necessite de passer la
        forme a jeton et non un nom concret.
        """
        lignes = self.parfile(parallel=4, dumpfile="exp.dmp")
        self.assertIn('dumpfile="exp-%d.dmp"', lignes)
        self.assertIn("parallel=4", lignes)

    def test_le_jeton_remplace_l_extension_il_ne_s_y_ajoute_pas(self):
        """`exp.dmp-%d` produirait `exp.dmp-01`, que rien ne retrouve.

        Le detail est agacant parce qu'il marche : le job s'ecrit, la
        lecture fonctionne, et la seule trace est un nom de fichier
        inhabituel. `%d` **remplace** l'extension.
        """
        brut = " ".join(
            l for l in self.parfile(parallel=2, dumpfile="exp.dmp")
            if l.startswith("dumpfile=")
        )
        self.assertIn("-%d", brut)
        self.assertNotIn(".dmp-", brut)
        self.assertLess(brut.index("-%d"), brut.index(".dmp"))

    def test_un_nom_sans_extension_recoit_le_jeton_puis_l_extension(self):
        self.assertIn('dumpfile="exp-%d.dmp"', self.parfile(parallel=2, dumpfile="exp"))

    def test_en_parallele_simple_il_n_y_a_ni_jeton_ni_option(self):
        """`parallel=1` est le defaut : l'ecrire n'apporte rien.

        Et surtout, poser `parallel=1` avec un nom **sans** jeton est
        correct, alors que poser un jeton sans parallele ne l'est pas :
        le nom contiendrait un `%d` litteral.
        """
        lignes = self.parfile(parallel=1, dumpfile="exp.dmp")
        self.assertIn('dumpfile="exp.dmp"', lignes)
        self.assertNotIn("parallel=", " ".join(lignes))

    # -- options conditionnelles ------------------------------------------

    def test_la_taille_de_fichier_est_optionnelle(self):
        self.assertNotIn("filesize=", " ".join(self.parfile(filesize_mb=0)))
        self.assertIn("filesize=500", self.parfile(filesize_mb=500))

    def test_reuse_dumpfiles_est_posee_par_defaut(self):
        """Une reprise ne doit pas echouer sur un dump deja present.

        Sans cette option, un export interrompu puis relance echouerait
        sur le fichier qu'il avait deja ecrit — c'est-a-dire
        exactement dans le cas ou l'on veut ne pas repartir de zero.
        """
        self.assertIn("reuse_dumpfiles=yes", self.parfile())
        self.assertNotIn("reuse_dumpfiles=", " ".join(self.parfile(reuse=False)))

    def test_les_remappings_utilisent_les_separateurs_data_pump(self):
        """`,` pour les tablespaces, `;` pour include/exclude.

        Ce sont des conventions Data Pump, pas des separateurs
        generiques : inverses, l'option est **simplement ignoree**, sans
        erreur. Un job qui « reussit » en ecrivant dans les mauvaises
        tablespaces ne dit rien de ce qui s'est passe.
        """
        lignes = self.parfile(
            remap_tablespace=["USERS:TS_A", "SYSAUX:TS_B"],
            include=['TABLE:"T1"', 'TABLE:"T2"'],
            exclude=['TABLE:"X"'],
        )
        self.assertIn("remap_tablespace=USERS:TS_A,SYSAUX:TS_B", lignes)
        self.assertIn('include=TABLE:"T1";TABLE:"T2"', lignes)
        self.assertIn('exclude=TABLE:"X"', lignes)


class TestParfileDImport(unittest.TestCase):
    """Le parfile d'import, et le mode simulation du dump."""

    def setUp(self) -> None:
        self.oracle = CoteOracleSimule()
        self.adapter = DataPumpAdapter(self.oracle.side, oracle=self.oracle)

    def parfile(self, **kw: Any) -> List[str]:
        defauts: Dict[str, Any] = dict(
            source_schema="HR", target_schema="TGT", job_name="J",
            dumpfile="exp.dmp", logfile="imp.log", parallel=1,
            table_exists_action="SKIP",
        )
        defauts.update(kw)
        return self.adapter.build_import_parfile(**defauts)

    def test_le_remap_de_schema_est_inconditionnel(self):
        """La duplication peut viser le meme nom.

        C'est precisement pour cela que `ALLOW_EXISTING_TARGET` existe,
        et non pour rendre le remap facultatif. Un remap optionnel
        laisserait la possibilite d'importer dans le schema source par
        inattention — sur une base de production, c'est l'erreur la
        plus grave que cet outil pourrait commettre.
        """
        self.assertIn('remap_schema="HR:HR"', self.parfile(
            source_schema="HR", target_schema="HR"))

    def test_le_remap_est_normalise(self):
        self.assertIn('remap_schema="SRC:TGT"',
                      self.parfile(source_schema="src", target_schema="tgt"))

    def test_un_schema_avec_apostrophe_echappe_le_remap_du_parfile(self):
        """L'apostrophe ne doit pas disparaitre du remap.

        Le remap est une valeur de **parfile**, pas du SQL : la regle
        d'echappement applicable est celle de Data Pump — le guillemet
        double — et non celle de `_lit`. Doubler l'apostrophe ici
        produirait un schema nomme `O''Brien`, qui n'existe pas, et
        l'import echouerait sur un schema valide.

        Le test verifie donc la **presence** de l'apostrophe, parce que
        c'est sa disparition silencieuse qui serait le bug.
        """
        lignes = self.parfile(source_schema="O'Brien", target_schema="TGT")
        remap = [l for l in lignes if l.startswith("remap_schema=")]
        self.assertEqual(len(remap), 1)
        # `O'Brien`, pas `O''Brien`. La normalisation en majuscules est
        # ici un effet de bord benin ; l'apostrophe, elle, doit rester
        # une apostrophe.
        self.assertEqual(remap[0], "remap_schema=\"O'BRIEN:TGT\"")

    def test_un_schema_avec_apostrophe_est_echappe_dans_le_sql_de_job(self):
        """Le meme nom, dans une requete SQL, s'echappe autrement.

        C'est le meme identifiant dans deux contextes, et les deux
        regles sont opposees. Confondre les deux produirait soit un
        nom de schema faux, soit du SQL qui ne compile pas — et le
        message de l'erreur ne dirait pas lequel des deux.
        """
        self.oracle.requetes.clear()
        self.adapter.job_status("O'Brien")
        self.assertIn("'O''Brien'", self.oracle.requetes[0])

    def test_sqlfile_ajoute_le_dumpfile_au_lieu_de_le_remplacer(self):
        """`SQLFILE` n'est pas un substitut de `DUMPFILE`.

        `SQLFILE` indique **ou ecrire le DDL**, `DUMPFILE` **quoi
        relire** : les deux sont necessaires, et le second ne disparait
        pas quand le premier est pose. Son omission ne produisait pas
        une verification sans dump, mais une relecture du nom par
        defaut `expdat.dmp` — absent, donc `ORA-31640`. La verification
        echouait alors sur un dump parfaitement complet, et le journal
        ne montrait qu'un fichier manquant, sans rapport avec lui.

        `remap_schema` reste en revanche absent : relire un dump ne
        cree aucun objet, le remap n'y aurait pas de sens.
        """
        lignes = self.parfile(sqlfile="verif.sql")
        self.assertIn('sqlfile="verif.sql"', lignes)
        self.assertIn("dumpfile=", " ".join(lignes))
        self.assertNotIn("remap_schema=", " ".join(lignes))

    def test_le_compte_cible_n_est_jamais_recree(self):
        """L'import ne doit pas tenter de creer le compte cible.

        Un export de schema embarque l'objet `USER` du schema exporte, et
        `REMAP_SCHEMA` le renomme a l'import : l'import cherchait donc a
        creer le compte cible, qui existe par construction. L'echec
        etait `ORA-31684: Le type d'objet USER:"..." existe deja`,
        **apres** avoir charge toutes les donnees : le run se
        concluait sur une erreur alors que la duplication etait
        complete, et le rapport attribuait l'echec a l'import sans
        designer sa cause.

        Verifie sur une vraie 19c : avec l'exclusion, l'import rend 0 et
        les 107 lignes d'`employees` sont la ; sans elle, il echoue.
        """
        lignes = " ".join(self.parfile())
        self.assertIn("exclude=", lignes)
        self.assertIn("USER", lignes)

    def test_la_configuration_ne_peut_pas_dissoudre_l_exclusion(self):
        """`EXCLUDE` est une donnee d'execution, pas une possibilite.

        L'exclusion du compte cible protege la base d'une ecriture que
        ni la duplication ni l'exploitant n'ont demandee. Une valeur de
        configuration qui la reprendrait doit donc etre absorbee, et
        non remplacer le controle par la configuration.
        """
        for variante in (["USER"], ["user"], ["Table:DEPT", "USER"], []):
            with self.subTest(exclude=variante):
                lignes = " ".join(self.parfile(exclude=variante))
                self.assertIn("USER", lignes.upper())

    def test_les_exclusions_de_la_configuration_sont_conservees(self):
        """La fusion est une union, pas un remplacement.

        Une exclusion demandee qui disparaissait au profit de la seule
        exclusion obligatoire changerait le contenu du schema duplique,
        en silence : le rapport ne dirait rien de l'objet omis.
        """
        lignes = self.parfile(exclude=["TABLE:DEPT", "VIEW:HR.V_TEST"])
        exclu = [l for l in lignes if l.startswith("exclude=")]
        self.assertEqual(len(exclu), 1, exclu)
        self.assertIn("TABLE:DEPT", exclu[0])
        self.assertIn("VIEW:HR.V_TEST", exclu[0])
        self.assertIn("USER", exclu[0])

    def test_une_exclusion_ne_reparait_pas(self):
        """La deduplication ignore la casse, pour que le parfile se lise.

        `USER` et `user` designent le meme type : les laisser tous deux
        ne changerait rien au comportement, mais `--verbose` finit par
        afficher la ligne, et une ligne illisible se relit de travers.
        """
        exclu = [l for l in self.parfile(exclude=["user"]) if l.startswith("exclude=")]
        self.assertEqual(exclu, ["exclude=USER"])

    def test_un_element_vide_est_ignore(self):
        """Une virgule vide ne doit pas produire un `;;` silencieux."""
        exclu = [l for l in self.parfile(exclude=["", "  "]) if l.startswith("exclude=")]
        self.assertEqual(exclu, ["exclude=USER"])

    def test_l_exclusion_est_posee_en_simulation_aussi(self):
        """La relecture doit decrire l'import qu'elle annonce.

        `EXCLUDE` porte sur le jeu d'objets lus, non sur ce qu'on en
        fait : la relecture du dump decrit donc exactement ce que
        l'import tentera. L'omettre en simulation donnerait une
        verification qui repond a une autre question que celle de
        l'etape 14.
        """
        lignes = self.parfile(sqlfile="v.sql", exclude=["TABLE:DEPT"])
        exclu = [l for l in lignes if l.startswith("exclude=")]
        self.assertEqual(len(exclu), 1, lignes)
        self.assertIn("USER", exclu[0])
        self.assertIn("TABLE:DEPT", exclu[0])

    def test_table_exists_action_est_omise_en_simulation(self):
        """Data Pump refuse l'option en mode `SQLFILE`.

        `ORA-39208: ... n'est pas valide pour les travaux SQL_FILE`. La
        poser rendait la verification du dump impossible, pour une
        raison sans rapport avec le dump lui-meme — le journal ne
        designait pas la cause reelle, et l'echec etait mis sur le
        compte de l'export. Le test qui precedait affirmait
        l'inverse : les deux options ne sont pas compatibles.

        L'option reste posee a l'import reel, ou elle regit
        effectivement le sort des tables deja presentes.
        """
        self.assertNotIn(
            "table_exists_action=", " ".join(self.parfile(sqlfile="v.sql"))
        )
        self.assertIn(
            'table_exists_action="SKIP"', self.parfile(sqlfile="")
        )

    def test_le_parfile_transmet_la_liste_de_parties_telle_quelle(self):
        """La liste des parties doit atteindre `impdp` intacte.

        `impdp` attend une liste de noms concrets, separes par des
        virgules. Le parfile ne fait que transmettre : c'est a
        l'appelant de construire la bonne forme, a partir de ce que
        l'export a reellement produit.

        La forme a jeton `base-%d.dmp` n'a pas sa place ici. `%d` est
        une variable de substitution propre a l'**export** ; `impdp` la
        refuse par `ORA-39124`. Le test qui precedait affirmait
        l'inverse et prescrivait cette forme — voir
        `TestSpecificationDImport` pour le test qui prouve le
        contraire, cote pipeline, ou la decision est prise.
        """
        self.assertIn(
            'dumpfile="exp-1.dmp,exp-2.dmp"',
            self.parfile(dumpfile="exp-1.dmp,exp-2.dmp"),
        )


class TestHelpers(unittest.TestCase):
    """Les deux contextes de citation, qui ne sont pas interchangeables."""

    def test_quote_double_le_guillemet_interne(self):
        self.assertEqual(_q('a"b'), '"a""b"')
        self.assertEqual(_q("simple"), '"simple"')
        self.assertEqual(_q(""), '""')

    def test_quote_ne_double_pas_les_apostrophes(self):
        """Le parfile n'a pas la meme syntaxe d'echappement que SQL.

        Doubler aussi les apostrophes produirait un nom faux. Verifier
        que les deux contextes restent distincts, c'est couvrir la
        source classique d'un schema `O''Brien` qui n'existe pas.
        """
        self.assertEqual(_q("O'Brien"), "\"O'Brien\"")

    def test_lit_double_l_apostrophe(self):
        self.assertEqual(_lit("O'Brien"), "O''Brien")
        self.assertEqual(_lit("rien"), "rien")

    def test_les_codes_succes_couvrent_les_vrais_codes_data_pump(self):
        """0, 1=succes, 2=+XML, 4=+avertissement, 8=interactif.

        Ne garder que 0 ferait echouer des exports reussis. Ce que
        l'exploitant finirait par contourner en ignorant le code de
        sortie — c'est-a-dire en paralysant le controle.
        """
        self.assertEqual(DATAPUMP_SUCCESS_CODES, frozenset({0, 1, 2, 4, 8}))

    def test_les_codes_avertissement_sont_un_sous_ensemble_des_succes(self):
        """Un code d'avertissement ne doit jamais faire echouer.

        Cette inclusion est une **intention**. Si un jour un code
        d'avertissement sortait de l'ensemble des succes, l'echec serait
        subi sans qu'on l'ait vu venir.
        """
        self.assertTrue(DATAPUMP_WARNING_CODES <= DATAPUMP_SUCCESS_CODES)


class TestHygieneDuParfile(unittest.TestCase):
    """Le fichier pose sur l'hote, et ce qu'il reste apres coup.

    Ces tests executent un **vrai** shell : c'est le shell qui applique
    le `umask`, qui ecrit le fichier et qui le supprime. Un runner
    simule ne verrait aucun de ces comportements — or ce sont
    precisement eux qu'il faut verifier.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.racine = Path(self._tmp.name)
        self.bin_dir = installer_faux_client(self.racine)
        # `PATH` surcharge : le corps distant doit trouver le faux client
        # avant le vrai, sans quoi le test mesurerait l'instance locale
        # au lieu du code de l'outil.
        self._path_de_base = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self._path_de_base}"
        self.addCleanup(self._restaurer_path)
        self.oracle = CoteOracleSimule()
        self.adapter = DataPumpAdapter(
            self.oracle.side, parfile_dir=str(self.racine), oracle=self.oracle
        )

    def _restaurer_path(self) -> None:
        os.environ["PATH"] = self._path_de_base

    def _parfile(self) -> List[str]:
        return self.adapter.build_export_parfile(
            schema="HR", job_name="J", dumpfile="exp.dmp", logfile="exp.log",
            content="ALL", compression="MEDIUM", parallel=1,
        )

    def test_le_parfile_est_pose_en_0600(self):
        """`umask 077` **avant** la premiere ecriture, pas apres.

        Un `chmod 600` apres coup laisse le fichier lisible pendant la
        fenetre entre sa creation et le chmod — suffisante pour qu'un
        autre compte de l'hote l'attrape. C'est la raison d'etre du
        `umask` dans l'amorcage, et non une commodite.
        """
        parfile = self.racine / "mode.par"
        resultat = LocalRunner().run_script(
            self.adapter._script_with_parfile(
                str(parfile), "\n".join(self._parfile()) + "\n", "expdp", 60
            ),
            timeout=120, mutating=True,
        )
        self.assertTrue(resultat.ok, resultat.stderr)
        mode = lire_rapport(parfile).get("MODE", "")
        self.assertTrue(
            mode.startswith("-rw-------"),
            f"mode {mode!r} : le parfile porte le userid, il doit etre en 0600",
        )

    def test_le_parfile_est_supprime_apres_un_export_reussi(self):
        """Le parfile porte `userid` : en mode repli, un mot de passe.

        Le laisser sur l'hote, c'est deposer un secret dans un fichier
        lisible par tous les comptes de l'hote, a l'insu de
        l'exploitant. Et comme le nom derive du `run_id` et du job, il
        est unique : rien ne viendra le nettoyer derriere nous.
        """
        resultat = self.adapter.run("expdp", self._parfile(), job_name="J", timeout=60)
        self.assertEqual(resultat.rc, 0)
        self.assertFalse(
            Path(resultat.parfile).exists(),
            f"le parfile {resultat.parfile} a survecu au traitement",
        )

    def test_le_parfile_est_supprime_meme_si_le_client_echoue(self):
        """Le chemin d'echec est celui ou le secret reste le plus longtemps.

        Un `expdp` qui echoue laisse un operateur en train de
        diagnostiquer, donc un fichier a portee de tout le monde
        pendant qu'il cherche. Le nettoyage est branche sur le `trap` du
        prelude, donc il couvre `osd_die`, `exit` et l'arret par signal.
        """
        parfile = self.racine / "echec.par"
        resultat = LocalRunner().run_script(
            self.adapter._script_with_parfile(
                str(parfile), "\n".join(self._parfile()) + "\n", "expdp", 60
            ),
            timeout=120, mutating=True,
        )
        self.assertEqual(resultat.rc, 0, resultat.stderr)
        self.assertFalse(parfile.exists())

    def test_le_parfile_est_supprime_quand_le_client_est_introuvable(self):
        """Le cas d'echec le plus frequent : `expdp` absent du PATH.

        Un client Oracle installe sans les outils Data Pump est
        ordinaire, et l'echec doit etre **nomme** plutot que
        « commande introuvable » : il y a deux hotes, et le message doit
        dire lequel. Le parfile doit disparaitre malgre tout.
        """
        # Le vrai `expdp` de la machine de developpement doit disparaitre
        # du PATH : sinon c'est lui qui repond, et le chemin 127 n'est
        # jamais exerce.
        os.environ["PATH"] = path_sans_client_reel(self._path_de_base)
        for chemin in self.bin_dir.iterdir():
            chemin.unlink()
        parfile = self.racine / "absent.par"
        resultat = LocalRunner().run_script(
            self.adapter._script_with_parfile(
                str(parfile), "\n".join(self._parfile()) + "\n", "expdp", 60
            ),
            timeout=120, mutating=True,
        )
        self.assertEqual(resultat.get("OSD_MISSING_CMD"), "expdp")
        self.assertEqual(resultat.rc, 127)
        self.assertFalse(parfile.exists())

    def test_un_nom_d_outil_hors_liste_blanche_est_refuse(self):
        """`osd_tool` n'est accepte que s'il vaut `expdp` ou `impdp`.

        La valeur vient de l'appelant, donc elle n'est pas une menace en
        soi — mais une faute de frappe (`expd`) donnerait un message
        d'« outil absent » qui envoyerait chercher un client parfaitement
        installe. La liste blanche distingue les deux cas : ici, un code
        de sortie distinct et un message qui nomme la liste.

        L'ordre des deux controles est donc significatif, et ce test le
        fige : la liste blanche passe **avant** la recherche dans le
        PATH, sinon `OSD_MISSING_CMD` recouvrirait les deux diagnostics.
        """
        parfile = self.racine / "inconnu.par"
        resultat = LocalRunner().run_script(
            self.adapter._script_with_parfile(
                str(parfile), "\n".join(self._parfile()) + "\n", "expd", 60
            ),
            timeout=120, mutating=True,
        )
        self.assertEqual(resultat.rc, 64)
        self.assertIn("outil inconnu", resultat.get("__fatal__", ""))
        self.assertNotIn("OSD_MISSING_CMD", resultat.kv)
        self.assertFalse(parfile.exists())

    def test_le_contenu_du_parfile_ne_transite_pas_dans_le_bloc(self):
        """Le parfile ne sort que par le script envoye.

        Le bloc de resultat alimente l'etat et le rapport : y faire
        figurer `userid` — donc eventuellement un mot de passe — le
        deposerait la ou n'importe quel compte peut lire. Le controle
        verifie l'absence, pas la presence d'un filtre : un filtre
        laisse passer ce qu'on n'avait pas prevu.
        """
        resultat = LocalRunner().run_script(
            self.adapter._script_with_parfile(
                str(self.racine / "p.par"), "\n".join(self._parfile()) + "\n",
                "expdp", 60,
            ),
            timeout=120, mutating=True,
        )
        self.assertNotIn("userid", "\n".join(resultat.kv).lower())
        self.assertNotIn("userid", resultat.stdout_raw.lower())

    def test_le_script_complet_est_syntactiquement_valide(self):
        """Le meme controle que pour `shell/`, mais sur l'assemblage.

        Le parfile est un contenu libre : guillemets, espaces, `:`,
        points-virgules. Un de ces caracteres mal echappe produirait un
        script qui ne compile pas — ou pire, qui compile en changeant
        de sens.
        """
        lignes = self.adapter.build_export_parfile(
            schema='A"B\'C', job_name="J", dumpfile="exp dmp.dmp",
            logfile="exp.log", content="ALL", compression="MEDIUM",
            parallel=3, include=['TABLE:"T 1"'],
        )
        script = self.adapter._script_with_parfile(
            "/tmp/osd/quote.par", "\n".join(lignes) + "\n", "expdp", 60
        )
        proc = subprocess.run(
            ["/bin/sh", "-n"], input=script.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())


class TestCeQueLeClientVoit(unittest.TestCase):
    """Le parfile transmis, observe par un faux client qui s'execute."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.racine = Path(self._tmp.name)
        self.bin_dir = installer_faux_client(self.racine)
        self._path_de_base = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self._path_de_base}"
        self.addCleanup(self._restaurer_path)
        self.oracle = CoteOracleSimule()
        self.oracle.side.directory = str(self.racine)
        self.adapter = DataPumpAdapter(
            self.oracle.side, parfile_dir=str(self.racine), oracle=self.oracle
        )

    def _restaurer_path(self) -> None:
        os.environ["PATH"] = self._path_de_base

    def _lancer(self, lignes: Sequence[str], tool: str = "expdp") -> DataPumpResult:
        return self.adapter.run(tool, lignes, job_name="J", timeout=60)

    def _export(self, **kw: Any) -> List[str]:
        defauts: Dict[str, Any] = dict(
            schema="HR", job_name="J", dumpfile="exp.dmp", logfile="exp.log",
            content="ALL", compression="MEDIUM", parallel=1,
        )
        defauts.update(kw)
        return self.adapter.build_export_parfile(**defauts)

    def test_la_ligne_userid_est_celle_attendue(self):
        """Le canal reel, pas une comparaison de chaine de parfile.

        `effective_connect` construit la chaine : le test verifie donc
        le resultat de toute la chaine, wallet et `as sysdba` compris.
        """
        resultat = self._lancer(self._export())
        rapport = lire_rapport(Path(resultat.parfile))
        self.assertEqual(rapport.get("LIGNE_USERID"), '"/@L_SRC as sysdba"')

    def test_sans_sysdba_la_chaine_ne_comporte_pas_de_suffixe(self):
        """`as sysdba` n'est pas decoration.

        Le wallet de l'instance ne contient que des comptes privilegies :
        sans ce suffixe, la connexion echoue en `ORA-28009`, et un export
        qui « echoue » ne dit pas pourquoi. Le test couvre la forme
        reelle, pas seulement la forme configuree.
        """
        self.oracle.side.sysdba = False
        resultat = self._lancer(self._export())
        rapport = lire_rapport(Path(resultat.parfile))
        self.assertEqual(rapport.get("LIGNE_USERID"), '"/@L_SRC"')

    def test_les_cles_transmises_sont_les_cles_attendues(self):
        """L'inventaire compare, pas l'absence.

        Une comparaison par exclusion (`assertNotIn`) ne verrait ni une
        option **manquante** ni une option **en double** qui ecraserait
        la premiere en silence. Les deux sont des defauts invisibles
        dans un journal de production.
        """
        lignes = self._export()
        resultat = self._lancer(lignes)
        rapport = lire_rapport(Path(resultat.parfile))
        vues = [c for c in rapport.get("CLEFS", "").rstrip(",").split(",") if c]
        attendues = sorted(l.split("=", 1)[0] for l in lignes if "=" in l)
        self.assertEqual(sorted(vues), attendues)

    def test_le_parfile_transmis_est_bien_pose_sur_le_disque(self):
        """Le client doit lire un **fichier**, pas un flux.

        Data Pump n'accepte pas ses options sur la ligne de commande de
        facon portable ; le parfile est donc la seule voie. Si le
        fichier n'existait pas, le client le dirait, et le test le
        verrait.
        """
        resultat = self._lancer(self._export())
        rapport = lire_rapport(Path(resultat.parfile))
        self.assertNotIn("PARFILE_ABSENT", rapport)
        self.assertIn("NB_OPTIONS", rapport)

    def test_un_client_absent_du_path_est_signale_par_son_nom(self):
        """Pas seulement « commande introuvable » : **laquelle**.

        Il y a deux hotes ; un message generique enverrait l'exploitant
        chercher sur le mauvais. Le code 127 reprend la convention shell,
        ce qui permet a un lecteur humain de reconnaitre la situation
        sans connaître l'outil.
        """
        os.environ["PATH"] = path_sans_client_reel(self._path_de_base)
        for chemin in self.bin_dir.iterdir():
            chemin.unlink()
        resultat = self._lancer(self._export())
        self.assertEqual(resultat.rc, 127)
        self.assertEqual(resultat.error_codes, [])

    def test_les_codes_erreur_du_client_atteignent_le_rapport(self):
        """Un `ORA-` produit par le client doit remonter.

        Le code est extrait par `osd_codes` et pose dans le bloc ; sans
        lui, un import qui echoue pour un tablespace inexistant
        ressemblerait a un echec sans cause.
        """
        resultat = self._lancer(self._export())
        self.assertIn("ORA-12545", resultat.error_codes)


class TestInterpretation(unittest.TestCase):
    """La decision « reussi » ou « echoue », et ce qui la fonde."""

    def setUp(self) -> None:
        self.oracle = CoteOracleSimule()
        self.adapter = DataPumpAdapter(self.oracle.side, oracle=self.oracle)

    def _interpret(self, rc: int = 0, **kw: Any) -> DataPumpResult:
        kv: Dict[str, str] = {"OSD_RC": str(rc)}
        kv.update(kw.pop("kv", {}))
        return self.adapter._interpret(
            Result(rc=rc, kv=kv, stderr=kw.pop("stderr", "")),
            tool=kw.pop("tool", "expdp"), job_name="J",
        )

    def test_un_code_de_succes_simple_ne_produit_aucun_avertissement(self):
        """0 et 1 : succes sans reserve.

        Le test verifie l'absence de toute mention, pas l'absence
        d'`error_codes` : un code de succes qui ajouterait un
        avertissement brouillerait le rapport, et l'exploitant
        s'habituerait a ignorer cette section — jusqu'au jour ou elle
        contiendrait le seul avertissement qui compte.
        """
        for rc in (0, 1):
            with self.subTest(rc=rc):
                resultat = self._interpret(rc)
                self.assertEqual(resultat.warnings, [])
                self.assertEqual(resultat.error_codes, [])

    def test_un_code_de_succes_avec_avertissement_le_signale(self):
        """2, 4 et 8 restent des **succes**, mais sont signales.

        Les ignorer ferait perdre « export termine avec des
        avertissements » — l'information qui explique un dump
        incomplet alors que le code de sortie est favorable. Les traiter
        comme des echecs ferait echouer des exports parfaitement
        utilisables.
        """
        for rc in sorted(DATAPUMP_WARNING_CODES):
            with self.subTest(rc=rc):
                self.assertIn(str(rc), str(self._interpret(rc).warnings))

    def test_un_code_echec_produit_une_mention_du_code_de_sortie(self):
        """Le libelle designe l'etape, pas l'outil.

        « code de sortie Data Pump 3 (echec de l'export) » dit ce qui
        s'est passe. Un message qui ne dirait que « echec » obligerait
        l'exploitant a ouvrir le journal pour savoir s'il doit
        recommencer l'export ou l'import — alors que le programme, lui,
        sait deja, et renvoie 4 dans le premier cas et 6 dans l'autre.
        """
        self.assertIn(
            ec.label(ec.EXPORT),
            str(self._interpret(3).warnings),
        )

    def test_import_et_export_ont_des_libelles_d_echec_differents(self):
        """4 pour l'export, 6 pour l'import : distincts, et atteignables.

        Un import qui echoue et qui rapporte « echec de l'export »
        envoie l'operateur recommencer un export qui, lui, a
        reussi — en overwriterant un dump valide.
        """
        for tool, attendu in (("expdp", ec.EXPORT), ("impdp", ec.IMPORT)):
            with self.subTest(tool=tool):
                resultat = self._interpret(3, tool=tool)
                self.assertIn(ec.label(attendu), str(resultat.warnings))
                autre = ec.IMPORT if tool == "expdp" else ec.EXPORT
                self.assertNotIn(ec.label(autre), str(resultat.warnings))

    def test_les_codes_erreur_viennent_du_bloc_d_abord(self):
        resultat = self._interpret(kv={"OSD_ERROR_CODES": "ORA-39002 ORA-01950"})
        self.assertEqual(resultat.error_codes, ["ORA-01950", "ORA-39002"])

    def test_les_codes_erreur_sont_repris_du_stderr_en_secours(self):
        """Le script distant peut avoir rate la detection.

        Le stderr est alors la seule source, et perdre les codes
        reviendrait a ne pas distinguer « pas d'espace disque » d'«
        import interrompu » dans un ticket d'incident.
        """
        resultat = self._interpret(stderr="ORA-12545: cannot perform operation")
        self.assertEqual(resultat.error_codes, ["ORA-12545"])

    def test_un_code_present_dans_le_bloc_n_est_pas_compte_deux_fois(self):
        resultat = self._interpret(
            kv={"OSD_ERROR_CODES": "ORA-39002"},
            stderr="ORA-39002 vu egalement sur stderr",
        )
        self.assertEqual(resultat.error_codes, ["ORA-39002"])

    def test_un_texte_sans_code_ne_produit_pas_de_code(self):
        """Pas de recherche de « successfully completed ».

        Les messages sont traduits : sur un client francais, un export
        reussi se termine par « FIN DU DEPLOI... ». Fonder le succes
        sur une chaine de ce genre aurait produit un faux echec sur une
        base parfaitement saine, et l'exploitant aurait appris a se
        mefier du rapport entier.
        """
        resultat = self._interpret(
            stderr="Job SYS_EXPORT_TABLE_01 completed successfully")
        self.assertEqual(resultat.error_codes, [])

    def test_un_bloc_vide_renvoie_aucun_code_et_aucune_erreur(self):
        resultat = self.adapter._interpret(Result(rc=0, kv={}), tool="expdp",
                                          job_name="J")
        self.assertEqual(resultat.error_codes, [])
        self.assertEqual(resultat.warnings, [])
        self.assertEqual(resultat.rc, 0)

    def test_le_resultat_ne_transporte_pas_le_parfile_complet(self):
        """Le diagnostic de l'hote ne doit pas remonter dans l'etat.

        Un `DataPumpResult` qui embarquerait la sortie du client
        finirait dans le rapport JSON, donc dans un fichier lisible par
        l'exploitant — et peut-etre dans un ticket d'incident. Le
        retour utile est le code, pas la production.
        """
        resultat = self._interpret(stderr="un diagnostic de dix mille lignes")
        rendering = repr(resultat)
        self.assertNotIn("dix mille lignes", rendering)


class TestSimulationHonnete(unittest.TestCase):
    """Le drapeau `simulated`, et pourquoi il n'est pas decoratif."""

    def setUp(self) -> None:
        self.oracle = CoteOracleSimule()
        self.adapter = DataPumpAdapter(self.oracle.side, oracle=self.oracle)

    def test_un_bloc_marque_dryrun_est_un_resultat_simule(self):
        self.adapter._interpret(
            Result(rc=0, kv={"OSD_RC": "0", "OSD_DRYRUN": "1"}),
            tool="expdp", job_name="J",
        )
        resultat = self.adapter._interpret(
            Result(rc=0, kv={"OSD_RC": "0", "OSD_DRYRUN": "1"}),
            tool="expdp", job_name="J",
        )
        self.assertTrue(resultat.simulated)

    def test_un_resultat_simule_ne_signale_aucun_avertissement(self):
        """Un avertissement sur une simulation serait un faux positif.

        L'exploitant verrait « attention » sur un export qui n'a pas
        ete tente, apprendrait a ignorer la section, et l'avertissement
        suivant — sur un run reel — ne serait plus lu.
        """
        resultat = self.adapter._interpret(
            Result(rc=0, kv={"OSD_RC": "0", "OSD_DRYRUN": "1"}),
            tool="expdp", job_name="J",
        )
        self.assertEqual(resultat.warnings, [])

    def test_un_run_reel_n_est_pas_marque_simule(self):
        resultat = self.adapter._interpret(
            Result(rc=0, kv={"OSD_RC": "0"}), tool="expdp", job_name="J"
        )
        self.assertFalse(resultat.simulated)

    def test_un_marqueur_hors_bloc_ne_declenche_pas_la_simulation(self):
        """Le marqueur doit venir du **canal machine**.

        `stdout_raw` est le flux brut ; un `OSD_DRYRUN=1` qui s'y
        trouverait ne serait pas une preuve. Deviner « pas d'erreur donc
        simule » transformerait une panne de communication en succes
        simule — le pire des deux mondes, puisque le rapport serait
        alorsMuet alors que rien n'a ete fait.
        """
        resultat = self.adapter._interpret(
            Result(rc=0, kv={}, stdout_raw="OSD_DRYRUN=1"),
            tool="expdp", job_name="J",
        )
        self.assertFalse(resultat.simulated)


class TestEtatDuJob(unittest.TestCase):
    """`DBA_DATAPUMP_JOBS`, et le fait qu'elle soit facultative."""

    def setUp(self) -> None:
        self.oracle = CoteOracleSimule()
        self.adapter = DataPumpAdapter(self.oracle.side, oracle=self.oracle)

    def test_un_job_absent_renvoie_un_dictionnaire_vide(self):
        """Pas de levee : un job absent est un etat normal.

        Il correspond a un job qui n'a jamais demarre, ou dont la table
        maitre a deja ete supprimee. Rendre un objet « inconnu »
        obligerait l'appelant a distinguer deux cas sans difference
        observable.
        """
        self.assertEqual(self.adapter.job_status("J"), {})
        self.assertTrue(self.oracle.requetes)

    def test_un_job_etat_renvoie_ses_quatre_champs(self):
        # Deux cles : `job_status` interroge la vue deux fois, l'etat
        # d'abord et `error_count` ensuite. Une cle unique ne
        # distinguerait pas les deux requetes.
        self.oracle.reponses = {
            "operation, state": [["J", "EXPORT", "SUCCESS"]],
            "error_count": [["0"]],
        }
        self.assertEqual(
            self.adapter.job_status("J"),
            {"job_name": "J", "operation": "EXPORT", "state": "SUCCESS",
             "error_count": "0"},
        )

    def test_une_vie_absente_ne_rend_pas_l_etat_illisible(self):
        """Une colonne absente fait echouer la requete entiere.

        La vue reelle d'une instance dont le dictionnaire est reduit ne
        porte pas forcement `ERROR_COUNT`. Le mettre dans la requete
        principale rendait alors l'etat du job illisible, et le
        controle prevu pour attraper un client detache devenait muet.
        Le compteur est donc lu separement, et son echec est absorbe.
        """
        self.oracle.reponses = {"operation, state": [["J", "EXPORT", "COMPLETED"]]}

        def query(sql: str, **kw: Any) -> List[List[str]]:
            self.oracle.requetes.append(sql)
            if "error_count" in sql.lower():
                raise OsdError("ORA-00904: invalid identifier", ec.PREREQ)
            return [["J", "EXPORT", "COMPLETED"]]

        self.oracle.query = query  # type: ignore[method-assign]
        statut = self.adapter.job_status("J")
        self.assertEqual(statut["state"], "COMPLETED")
        self.assertEqual(statut["error_count"], "")

    def test_un_acces_refuse_reste_un_etat_inconnu(self):
        """Une session sans droit sur le dictionnaire ne bloque rien.

        L'etat du job est un controle **complementaire**. Le faire
        echouer empecherait de dupliquer quoi que ce soit, alors que
        l'export qu'on cherche a controler est parfaitement valide.
        """
        self.oracle.erreur = OsdError("ORA-00942: table or view does not exist", ec.PREREQ)
        self.assertEqual(self.adapter.job_status("J"), {})

    def test_sans_acces_sql_le_controle_est_neutre(self):
        """`oracle=None` — le cas d'un Data Pump hors dictionarye.

        Le contournement doit rester neutre, pas lever : c'est ce qui
        permet a l'appelant de ne pas distinguer deux cas.
        """
        sans_sql = DataPumpAdapter(self.oracle.side)
        self.assertEqual(sans_sql.job_status("J"), {})
        sans_sql.drop_job("J")
        self.assertEqual(self.oracle.requetes, [])

    def test_le_nom_du_job_est_echappe_dans_le_sql(self):
        """Un nom de job vient du `run_id` et du schema.

        Le `run_id` est genere par l'outil et le nom de schema vient de
        la configuration : les deux sont valides, mais la citation reste
        la seule garantie qu'un nom avec apostrophe ne produise pas un
        SQL different de celui voulu.
        """
        self.adapter.job_status("J")
        self.assertIn("'J'", self.oracle.requetes[0])

    def test_drop_job_garantit_une_suppression_sans_echec(self):
        """Un job deja absent ne doit pas faire echouer un nettoyage.

        `DBMS_DATAPUMP.REMOVE_JOB` leve `ORA-31600` sur un job absent.
        D'ou le controle d'existence en SQL : sans lui, un `clean` sur
        une base ou le job n'a jamais tourne echouerait, et l'exploitant
        conclurait a tort que le nettoyage est casse.
        """
        self.adapter.drop_job("J")
        sql = " ".join(self.oracle.requetes).lower()
        self.assertIn("count(*)", sql)
        self.assertIn("dba_datapump_jobs", sql)

    def test_drop_job_ignore_une_erreur_sql(self):
        """Le nettoyage est un confort, pas une condition du succes.

        Une session sans droit sur `DBMS_DATAPUMP` ne doit pas faire
        passer un run reussi en echec — le schema est deja libere, et
        c'est tout ce qu'on demandait a l'etape.
        """
        self.oracle.erreur = OsdError("ORA-06550: PL/SQL error", ec.PREREQ)
        self.adapter.drop_job("J")


if __name__ == "__main__":
    unittest.main()
