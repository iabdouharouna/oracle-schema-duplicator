"""Data Pump : export, verification du dump, import.

Ce module est le seul endroit du projet qui connait la syntaxe
`expdp`/`impdp`. Trois decisions y sont appliquees, chacune consequence
directe d'une contrainte Oracle documentee :

1. **Tout passe par un parfile.** Les options sont nombreuses et
   contiennent des espaces, des quotes et des `:`. Les ecrire sur la
   ligne de commande rendrait tout quoting hasardeux, et `--%none` n'est
   pas portable. Le parfile est ecrit en `0600` sur l'hote et supprime
   en sortie.

2. **Le succes n'est pas lu dans le texte du journal.** Les messages
   Data Pump sont traduits — sur un client francais, un export reussi se
   termine par « FIN DU DEPLOI... ». Le resultat est donc determine par
   le code de sortie, la presence d'un code `ORA-`/`UDI-`, et l'etat du
   job dans `DBA_DATAPUMP_JOBS`.

3. **La verification du dump passe par `SQLFILE`.** `impdp SQLFILE=`
   relit le dump et produit le DDL sans rien ecrire en base. C'est la
   seule maniere de prouver qu'un dump est complet avant de laisser
   `impdp` ecrire dans le schema cible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .. import exit_codes as ec
from ..errors import OsdError
from ..logging_setup import get_logger
from ..runner import Raw, Result, _single_quote, build_script, load_body

LOG = get_logger()

#: Codes de retour Data Pump considers comme succes. Data Pump utilise
#: plusieurs codes pour un meme succes (1=succes normal, 2=succes avec
#: avertissement XML, 4=succes avec avertissement, 8=succec du client
#: interactif). Les propager tels quels ferait echouer un export reussi.
DATAPUMP_SUCCESS_CODES = frozenset({0, 1, 2, 4, 8})

#: Codes qui signifient « le job a bien tourne, mais il y a un probleme ».
DATAPUMP_WARNING_CODES = frozenset({2, 4, 8})

#: Etats de `DBA_DATAPUMP_JOBS.STATE` qui signifient « le travail est
#: fini ». Volontairement unique : tous les autres etats — `RUNNING`,
#: `EXECUTING`, `STOPPING`, `FAILED`, `NEEDS_COMMIT` — signifient que le
#: dump n'est pas complet, et l'ignorer reviendrait a valider un fichier
#: encore en ecriture. C'est la seule conclusion sure, `COMPLETED`
#: etant l'etat final documente d'un job termine sans erreur.
DATAPUMP_OK_STATES = frozenset({"COMPLETED"})

#: Remedes specifiques aux codes Oracle dont la cause est **previsible**
#: et ne se trouve pas dans le journal.
#:
#: Le remede generique — « consulter le journal Data Pump sur l'hote »
#: — est la seule chose que l'outil peut dire quand il ne connait pas la
#: cause. Il est ici inutile, parce que l'echec est entierement
#: deterministe : l'objet existe, `TABLE_EXISTS_ACTION` ne s'applique
#: qu'aux tables, et la suite n'est qu'une question de combien de types
#: d'objets on laisse deriver.
#:
#: Ce sont des faits de la 19c, observes et non supposes :
#:
#: - `ORA-31684` se produit sur un objet **deja present**. Data Pump
#:   n'a d'option que pour les tables (`TABLE_EXISTS_ACTION`) : les
#:   sequences, procedures, index, triggers et vues d'un schema
#:   partiellement peuple echouent donc tous, et le journal en nomme un
#:   par un sans dire qu'il y en a d'autres.
#: - `ORA-39111` accompagne `ORA-31684` sur un import interrompu par
#:   cette collision : il signale l'arret, pas une seconde cause.
#:
#: La table est volontairement vide plutot que devinee pour tout code
#: rencontre : une remediation inventee est pire que la neutrality, car
#: elle oriente l'exploitant vers une cause fausse.
DATAPUMP_REMEDES: Dict[str, str] = {
    "ORA-31684": (
        "Un objet du schema cible existe deja, et l'import s'y arrete. "
        "TABLE_EXISTS_ACTION ne s'applique qu'aux TABLES : les sequences, "
        "procedures, index, triggers et vues d'un schema partiellement "
        "peuple echouent tous. Pour une duplication complete, viser un "
        "schema cible vide, ou le recreer au prealable : "
        "DROP USER <cible> CASCADE ; CREATE USER <cible> ... puis "
        "ALLOW_EXISTING_TARGET=true. Pour une reprise partielle, "
        "TABLE_EXISTS_ACTION=SKIP conserve les tables mais ne touche ni "
        "aux autres objets ni aux donnees deja chargees."
    ),
    "ORA-39111": (
        "L'import s'est arrete en cours de route. Ce code en est la "
        "consequence, pas une cause distincte : lire le code qui le "
        "precede dans le journal."
    ),
}

#: Types d'objets que l'import n'a **jamais** le droit de creer, quel
#: que soit `EXCLUDE`.
#:
#: `USER` : un export de schema (`SCHEMAS=HR`) embarque l'objet `USER`
#: du schema exporte, et `REMAP_SCHEMA=HR:OSDTEST` le renomme au
#: moment de l'import. L'import cherchait donc a creer le compte cible,
#: qui existe par construction — l'etape 7 exige qu'il existe, et son
#: etat initial releve de l'initialisation de la base, pas de la
#: duplication. L'echec etait `ORA-31684: Le type d'objet USER:"..."
#: existe deja`, **apres** avoir charge toutes les donnees : le run se
#: concluait sur une erreur alors que la duplication etait complete, et
#: le rapport mettait l'echec sur le compte de l'import sans designer
#: sa cause.
#:
#: L'exclusion est donc posee par l'outil et non par la configuration :
#: un `EXCLUDE` contenant `USER` ne doit pas pouvoir la desamorcer.
EXCLUDE_A_IMPORT = ("USER",)

_ERROR_RE = re.compile(r"(?:ORA|UDI|DBMGSPC)-\d+")

#: Timeout de l'etape de verification SQLFILE. La relecture du dump est
#: proportionnelle a sa taille ; 1 h par defaut laisse le temps a un
#: schema de plusieurs giga-octets sans bloquer indefiniment.
VERIFY_TIMEOUT = 3600

#: Extension des fichiers produits par Data Pump. Utilisee pour poser le
#: jeton `%d` au bon endroit : il remplace l'extension, il ne s'y ajoute pas.
_DUMP_EXT = ".dmp"


@dataclass
class DumpPart:
    """Un fichier du dump exporte."""

    name: str
    bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "bytes": self.bytes}


@dataclass
class DataPumpResult:
    """Resultat structure d'une operation Data Pump."""

    rc: int
    job_name: str
    parts: List[DumpPart] = field(default_factory=list)
    error_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    job_status: str = ""
    parfile: str = ""
    #: L'operation a-t-elle ete simulee plutot qu'executee ?
    #:
    #: Ce drapeau n'est pas decoratif : sans lui, un dry-run rapporterait
    #: « export reussi, 0 partie » et « dump relu integralement, DDL
    #: regenerable » — deux affirmations **fausses**, produites par un
    #: code de retour nul. Un rapport qui ment sur ce qu'il a verifie est
    #: pire qu'un rapport absent : l'exploitant s'appuiera dessus pour
    #: decider qu'il peut y aller, et il aura tort.
    simulated: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(p.bytes for p in self.parts)


class DataPumpAdapter:
    """Pilote expdp et impdp sur un cote de la duplication."""

    def __init__(
        self, side, *, parfile_dir: str = "", oracle: Any = None
    ) -> None:
        """`side` porte la configuration du cote, `oracle` son acces SQL.

        Les deux sont distincts et le sont restes : `OracleSide` ne
        contient que des donnees (connect, schema, directory, runner),
        tandis que `OracleAdapter` construit et fait executer les
        scripts SQL. Les methodes qui interrogent le dictionnaire des
        jobs appelaient `self.side.query()` — attribut qui n'existe
        pas, donc `AttributeError`, qui n'est **pas** une `OsdError` et
        donc echappait au `except OsdError` de l'appelant : l'etape 11
        (« Controler l'etat du job ») se interrompait sur une trace
        Python au lieu de conclure.

        `oracle` est optionnel, et c'est deliberement : le diagnostic
        doit rester un complement. Une session sans droit sur
        `DBA_DATAPUMP_JOBS` rend un etat « inconnu », pas un echec, car
        l'export qu'on cherche a controler est parfaitement valide.
        """
        self.side = side
        self.oracle = oracle
        self.parfile_dir = parfile_dir or "/tmp"

    # -- Construction du parfile -----------------------------------------
    def build_export_parfile(
        self,
        *,
        schema: str,
        job_name: str,
        dumpfile: str,
        logfile: str,
        content: str,
        compression: str,
        parallel: int,
        filesize_mb: int = 0,
        remap_tablespace: Sequence[str] = (),
        exclude: Sequence[str] = (),
        include: Sequence[str] = (),
        reuse: bool = True,
    ) -> List[str]:
        """Retourne les lignes du parfile d'export.

        La construction se fait ligne par ligne, avec citation explicite.
        Le parfile n'est jamais genere depuis une chaine concatenee puis
        evaluee : chaque valeur est ecrite entre quotes, ce qui la rend
        litterale pour Data Pump.
        """
        lines: List[str] = [f'userid="{_userid(self.side)}"']
        lines.append(f"schemas={_q(schema.upper())}")
        lines.append(f"directory={_q(self.side.directory)}")
        lines.append(f"logfile={_q(logfile)}")
        lines.append(f"content={_q(content)}")
        lines.append(f"compression={_q(compression)}")

        # PARALLEL > 1 impose le jeton %d : Data Pump produit alors
        # `base-01.dmp`, `base-02.dmp`, ... Le jeton remplace l'extension,
        # il ne s'ajoute pas apres elle. Les noms de fichiers ne peuvent
        # donc pas etre devines a l'avance : ils sont decouverts apres
        # coup, par enumeration du repertoire.
        #
        # Le mot-cle est `PARALLEL` et non `PARALLELISM` : verifie sur
        # `expdp help=y` en 19.20, ou `PARALLELISM` n'existe pas et
        # provoquerait LRM-00101.
        if parallel > 1:
            base = dumpfile[: -len(_DUMP_EXT)] if dumpfile.endswith(_DUMP_EXT) else dumpfile
            lines.append(f"dumpfile={_q(base + '-%d' + _DUMP_EXT)}")
            lines.append(f"parallel={parallel}")
        else:
            lines.append(f"dumpfile={_q(dumpfile)}")

        if filesize_mb > 0:
            lines.append(f"filesize={filesize_mb}")
        if remap_tablespace:
            lines.append("remap_tablespace=" + ",".join(remap_tablespace))
        if include:
            lines.append("include=" + ";".join(include))
        if exclude:
            lines.append("exclude=" + ";".join(exclude))
        if reuse:
            # Un run repris ne doit pas echouer sur un dump deja present.
            lines.append("reuse_dumpfiles=yes")
        return lines

    def build_import_parfile(
        self,
        *,
        source_schema: str,
        target_schema: str,
        job_name: str,
        dumpfile: str,
        logfile: str,
        parallel: int,
        table_exists_action: str,
        remap_tablespace: Sequence[str] = (),
        exclude: Sequence[str] = (),
        include: Sequence[str] = (),
        sqlfile: str = "",
    ) -> List[str]:
        """Retourne les lignes du parfile d'import.

        `sqlfile` non vide transforme l'import en simulation : Data Pump
        ecrit le DDL dans un fichier et ne touche pas a la base. C'est le
        mode utilise pour la verification du dump et pour l'apercu
        `dry-run --with-sqlfile`.

        `dumpfile` est emis dans **les deux** cas, et c'est indispensable.
        `SQLFILE` ne remplace pas `DUMPFILE` : il indique ou ecrire le
        DDL, pas quoi relire. Omettre `DUMPFILE` ne produit pas une
        verification sans dump, mais une tentative de relire le nom par
        defaut `expdat.dmp` — absent, donc ORA-31640. La verification
        echouait alors sur un dump parfaitement complet, en remettant en
        cause l'export a l'etape 12 ; le journal ne montrait pourtant
        qu'une relecture de fichier inexistant, sans rapport avec lui.

        `TABLE_EXISTS_ACTION` n'est pas pose en mode `SQLFILE` : Data
        Pump le refuse explicitement, `ORA-39208: ... n'est pas valide
        pour les travaux SQL_FILE`. Il n'a de toute facon aucun effet
        la-dessus, puisqu'aucun objet n'est cree. Le poser rendait la
        verification impossible, et pour une raison sans rapport avec
        le dump — a nouveau un echec dont le journal ne designait pas
        la cause reelle.

        `EXCLUDE` est pose meme en mode `SQLFILE`, et c'est intentionnel :
        l'exclusion porte sur le jeu d'objets lus, non sur ce qu'on en
        fait. La relecture du dump doit donc decrire exactement ce que
        l'import tentera, faute de quoi la verification validerait un
        import auquel elle n'a pas essaye de repondre.

        Le remap de schema, lui, reste propre a l'import reel : relire
        un dump ne cree aucun objet.

        `dumpfile` est utilise tel quel. Pour un dump produit en
        plusieurs parties, l'appelant doit passer la **liste des noms
        reels**, separes par des virgules — et non la forme a jeton
        `base-%d.dmp` : `%d` est une variable de substitution propre a
        l'export, et `impdp` la refuse (`ORA-39124`). Cette methode ne
        valide pas la forme, elle ne fait que la transmettre : c'est a
        l'appelant de construire la bonne, a partir de ce que
        l'export a reellement produit.
        """
        lines: List[str] = [f'userid="{_userid(self.side)}"']
        lines.append(f"directory={_q(self.side.directory)}")
        lines.append(f"logfile={_q(logfile)}")
        lines.append(f"parallel={parallel}")
        lines.append(f"dumpfile={_q(dumpfile)}")
        if sqlfile:
            lines.append(f"sqlfile={_q(sqlfile)}")
        else:
            lines.append(f"table_exists_action={_q(table_exists_action)}")
            # Le remap de schema est inconditionnel : la duplication peut
            # viser le meme nom, c'est pourquoi ALLOW_EXISTING_TARGET
            # existe, pas pourquoi le remap serait optionnel.
            lines.append(f"remap_schema={_q(source_schema.upper() + ':' + target_schema.upper())}")
            if remap_tablespace:
                lines.append("remap_tablespace=" + ",".join(remap_tablespace))
            if include:
                lines.append("include=" + ";".join(include))
        # `EXCLUDE` est pose dans les deux modes : il porte sur le jeu
        # d'objets **lus**, non sur ce qu'on en fait. La relecture doit
        # donc decrire exactement ce que l'import tentera, faute de quoi
        # la verification de l'etape 12 validerait un import auquel
        # elle n'a pas essaye de repondre.
        excl = _exclude_import(exclude)
        if excl:
            lines.append("exclude=" + ";".join(excl))
        return lines

    # -- Execution --------------------------------------------------------
    def run(
        self,
        tool: str,
        parfile_lines: Sequence[str],
        *,
        job_name: str,
        timeout: Optional[int] = None,
    ) -> DataPumpResult:
        """Ecrit le parfile sur l'hote, l'execute, puis le supprime.

        La suppression est faite meme en cas d'echec : un parfile peut
        contenir une chaine `userid`, et il ne doit pas survivre au
        traitement.
        """
        parfile_path = f"{self.parfile_dir}/osd_{tool}_{job_name}.par"
        parfile_body = "\n".join(parfile_lines) + "\n"
        script = self._script_with_parfile(parfile_path, parfile_body, tool, timeout)

        LOG.info(
            "%s sur %s (job %s, %d options)",
            tool, self.side.label(), job_name, len(parfile_lines),
        )
        # `mutating=True` : `expdp`/`impdp` ecrivent des fichiers, lancent
        # un job dans le dictionnaire, et `impdp` modifie le schema cible.
        # Ce sont les seules operations du projet qui ne soient pas
        # reversibles par une suppression de fichier, et les seules pour
        # lesquelles « ne rien faire » est l'engagement pris par
        # `--dry-run`. Le parfile est ecrit puis supprime par le script
        # lui-meme ; en simulation il ne l'est pas, il n'est jamais cree.
        result = self.side.runner.run_script(script, timeout=timeout, mutating=True)
        parsed = self._interpret(result, tool=tool, job_name=job_name)
        parsed.parfile = parfile_path
        return parsed

    def _script_with_parfile(
        self, parfile_path: str, parfile_content: str, tool: str, timeout: Optional[int]
    ) -> str:
        """Assemble le script distant, parfile compris.

        Le contenu du parfile est libre : quotes, points-virgules,
        deux-points, espaces. Il est donc place dans une variable entre
        simples quotes par le mecanisme general du runner, puis ecrit par
        l'amorcage ci-dessous.

        Aucun `echo` n'est employe : `printf %s` ecrit le contenu octet
        pour octet, sans interpretation, et le `umask 077` garantit le
        mode 0600 avant meme la premiere ecriture.
        """
        bootstrap = "\n".join([
            "# Ecriture du parfile en 0600, avant toute execution.",
            f"osd_parpath={_single_quote(parfile_path)}",
            f"osd_parcontent={_single_quote(parfile_content)}",
            '(umask 077 && printf %s "$osd_parcontent" > "$osd_parpath") \\',
            "    || osd_die 'ecriture du parfile impossible' 73",
        ])
        argv = [tool, Raw('"$osd_parpath"'), str(timeout or 0)]
        return build_script(
            load_body("remote_datapump.sh"), argv,
            bootstrap=bootstrap, env=self.side.env(),
        )

    def _interpret(self, result: Result, *, tool: str, job_name: str) -> DataPumpResult:
        """Traduit le bloc distant en resultat metier.

        Trois conditions doivent etre reunies pour declarer une reussite :
        code de sortie Data Pump, absence d'erreur Oracle, et etat du job.
        Une seule ne suffit pas — c'est precisement la combinaison qui
        evite les faux positifs et les faux negatifs.
        """
        rc = result.get_int("OSD_RC", result.rc)
        codes = sorted(set(
            re.findall(r"(?:ORA|UDI|DBMGSPC)-\d+", result.get("OSD_ERROR_CODES", ""))
        ))
        if not codes:
            codes = sorted(set(_ERROR_RE.findall(result.stderr)))

        dp_error = result.get("OSD_DATAPUMP_ERROR") == "1"
        out = DataPumpResult(rc=rc, job_name=job_name, error_codes=codes)

        # Le `NullRunner` marque sa reponse. Aucun code n'ayant ete
        # consulte, aucune erreur ne peut etre constatee, et `rc` vaut 0
        # par construction : sans cette detection, l'absence d'erreur
        # serait indiscernable d'un succes reel.
        if result.get("OSD_DRYRUN") == "1":
            out.simulated = True
            return out

        if rc not in DATAPUMP_SUCCESS_CODES:
            out.warnings.append(
                f"code de sortie Data Pump {rc} "
                f"({ec.label(_code_for(tool))})"
            )
        if rc in DATAPUMP_WARNING_CODES:
            out.warnings.append(f"Data Pump a signale des avertissements (rc={rc})")
        if dp_error and codes:
            out.warnings.append("codes d'erreur dans la sortie: " + ", ".join(codes))

        return out

    # -- Verification du dump --------------------------------------------
    def verify_dump(
        self,
        *,
        source_schema: str,
        target_schema: str,
        dumpfile: str,
        job_name: str,
        parallel: int,
        table_exists_action: str,
        remap_tablespace: Sequence[str] = (),
        sqlfile_name: str = "",
        logfile_name: str = "",
    ) -> DataPumpResult:
        """Relit le dump avec `SQLFILE` sans rien ecrire en base.

        Cette etape est la seule qui prouve qu'un dump est complet. Sur
        un dump interrompu, elle echoue explicitement :

            ORA-39002: invalid operation
            ORA-39059: incomplete dump file set
            ORA-39246: master table not found in dump file set

        Ces trois codes ont ete observes sur un export interrompu : la
        detection ne repose sur aucun texte traduit.

        `sqlfile_name` et `logfile_name` sont fournis par l'appelant,
        qui les a deja annonces dans ses artefacts : le nettoyage doit
        pouvoir les nommer, et un nom reconstruit ici a l'identique
        n'est verifie nulle part — il divergeait silencieusement, et le
        DDL de chaque run restait dans le repertoire DIRECTORY.
        """
        sqlfile = sqlfile_name or f"osd_verify_{job_name}.sql"
        parfile = self.build_import_parfile(
            source_schema=source_schema,
            target_schema=target_schema,
            job_name=job_name,
            dumpfile=dumpfile,
            logfile=logfile_name or f"osd_verify_{job_name}.log",
            parallel=parallel,
            table_exists_action=table_exists_action,
            remap_tablespace=remap_tablespace,
            sqlfile=sqlfile,
        )
        return self.run("impdp", parfile, job_name=f"{job_name}_VER", timeout=VERIFY_TIMEOUT)

    # -- Etat du job ------------------------------------------------------
    def job_status(self, job_name: str) -> Dict[str, str]:
        """Interroge `DBA_DATAPUMP_JOBS` pour un job donne.

        Indispensable parce que le client Data Pump peut se detacher
        (`EXIT_CLIENT`) en laissant le job tourner : le code de sortie du
        client ne suffit alors plus a conclure.

        Rend un dictionnaire **vide** si l'etat est inconnu — job absent,
        privilege insuffisante, session SQL impossible. L'etat du job
        est un controle complementaire, jamais un prerequis : faire
        echouer l'etape 11 parce que `DBA_DATAPUMP_JOBS` est illisible
        empecherait de dupliquer quoi que ce soit, alors que l'export
        est lui parfaitement valide.

        `ERROR_COUNT` est lu par une **seconde requete**, et son echec
        est absorbe. Il ne doit pas etre dans la requete principale
        parce qu'une colonne absente la fait echouer en entier : une
        vue `DBA_DATAPUMP_JOBS` amputee — observee sur une 19c
        d'inventaire reduit, ou n'en subsistent que les colonnes
        d'identification — rendait alors l'etat du job illisible, et le
        controle prevu pour attraper un client detache devenait muet.
        A l'inverse, `STATE` est indispensable et n'est jamais
       Tolere absent : sans lui, aucun etat n'est connu.
        """
        oracle = self.oracle
        if oracle is None:
            return {}
        ou = f"where job_name = '{_lit(job_name)}'"
        try:
            rows = oracle.query(
                "select job_name, operation, state "
                f"from dba_datapump_jobs {ou}"
            )
        except OsdError:
            return {}
        if not rows:
            return {}
        row = rows[0]
        status = {
            "job_name": row[0] if len(row) > 0 else "",
            "operation": row[1] if len(row) > 1 else "",
            "state": row[2] if len(row) > 2 else "",
            "error_count": "",
        }
        try:
            compte = oracle.query(
                f"select error_count from dba_datapump_jobs {ou}"
            )
        except OsdError:
            compte = []
        if compte and len(compte[0]) > 0:
            status["error_count"] = str(compte[0][0])
        return status

    def drop_job(self, job_name: str) -> None:
        """Supprime la table maitre d'un job, pour liberer le schema.

        A n'appeler que sur un job termine : detacher un job en cours
        n'a aucun effet sur son deroulement.

        L'appel est tolerant : l'absence d'acces au dictionnaire, ou un
        job deja absent, ne doit pas faire echouer un nettoyage — le but
        de l'etape est precisement de ne rien laisser derriere. Le
        controle d'existence est fait en SQL explicite plutot que par un
        `exception when others`, qui masquerait toute erreur reelle
        derriere le `ORA-31600` attendu.
        """
        oracle = self.oracle
        if oracle is None:
            return
        try:
            oracle.execute(
                "declare\n"
                "  l_cnt integer;\n"
                "begin\n"
                "  select count(*) into l_cnt from dba_datapump_jobs "
                f"where job_name = '{_lit(job_name)}';\n"
                "  if l_cnt > 0 then\n"
                "    dbms_datapump.remove_job;\n"
                "  end if;\n"
                "end;\n"
            )
        except OsdError as erreur:
            LOG.debug("drop_job %s ignore : %s", job_name, erreur)


def _code_for(tool: str) -> int:
    return ec.EXPORT if tool == "expdp" else ec.IMPORT


def _q(value: str) -> str:
    """Quote une valeur de parfile.

    Data Pump interprete les quotes du parfile : une valeur contenant un
    guillemet doit voir ce guillemet double, sinon la ligne est coupee.
    """
    return '"' + str(value).replace('"', '""') + '"'


def _exclude_import(exclude: Sequence[str]) -> List[str]:
    """Fusionne `EXCLUDE_A_IMPORT` et les exclusions de la configuration.

    L'ordre est significatif pour la lecture du parfile, pas pour Data
    Pump : les exclusions obligatoires passent en premier, donc la ligne
    affichee dit d'elle-meme ce que l'outil refuse de faire.

    La deduplication ignore la casse, parce que `USER` et `user`
    désignent le meme type d'objet : les laisser tous deux ne changerait
    rien au comportement, mais rendrait le parfile et le journal
    difficiles a relire, et `--verbose` finit toujours par afficher la
    ligne.

    Aucun element fourni n'est retire, et aucun obligatoire n'est
    ajoute deux fois : c'est la seule maniere de garantir que la
    configuration ne peut pas dissoudre une exclusion qui protege la
    cible.
    """
    out: List[str] = []
    vues: set = set()
    for element in list(EXCLUDE_A_IMPORT) + list(exclude):
        element = str(element).strip()
        if not element:
            continue
        cle = element.upper()
        if cle in vues:
            continue
        vues.add(cle)
        out.append(element)
    return out


def _lit(value: str) -> str:
    return str(value).replace("'", "''")


def _userid(side) -> str:
    """Chaine `userid` du parfile.

    En mode wallet, aucune valeur ne contient de secret. En mode repli,
    le mot de passe est ici — et nulle part ailleurs : pas dans la ligne
    de commande, pas dans les journaux, qui passent tous par `redact`.
    """
    return side.effective_connect()
