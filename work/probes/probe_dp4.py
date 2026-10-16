"""Sondes : deux `query=` dans un meme parfile (mode B multi-tables), et
une seule clause `exclude=TABLE:"IN ('A', 'B')"` (mode A multi-noms).

Deux formes utilisees par l'outil, dont la deuxieme n'a pas ete mesuree
telle quelle lors des sondes R1-R5.
"""
import sys

sys.path.insert(0, "src")

from osd import config
from osd.adapters.datapump import DataPumpAdapter
from osd.runner import build_script, load_body
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load("config/config.conf")
p = Pipeline(cfg=cfg, state=State(run_id="PROBE"), run_id="PROBE", only=[])

side = p.source
pump = DataPumpAdapter(side, oracle=p.source_adapter)
DIRPATH = p.source_adapter.directory_path(side.directory)


def remote(argv, timeout=300):
    return side.runner.run_script(build_script(load_body("remote_exec.sh"), argv),
                                  timeout=timeout)


def exp(nom, extra, cibles):
    lines = [
        f'userid="{side.effective_connect()}"',
        'schemas="HR"',
        f'directory="{side.directory}"',
        f'logfile="osdp{nom}.log"',
        'content="ALL"',
        'compression="ALL"',
        f'job_name="OSDP{nom}"',
        f'dumpfile="osdp{nom}.dmp"',
    ]
    lines.extend(extra)
    print(f"--- P{nom} : {extra}", flush=True)
    r = pump.run("expdp", lines, job_name=f"OSDP{nom}", timeout=900)
    print(f"    rc={r.rc} erreurs={r.error_codes}", flush=True)
    erreurs = remote(["grep", "-n", "-E", "ORA-|UDE-|LRM-",
                      f"{DIRPATH}/osdp{nom}.log"])
    print("    journal erreurs :", erreurs.rows[:6] if erreurs.rc == 0
          else "(aucune)", flush=True)
    lignes = remote(["grep", "exported", f"{DIRPATH}/osdp{nom}.log"])
    print(f"    tables visees ({cibles}) :", flush=True)
    for ligne in lignes.rows:
        if any(c in ligne for c in cibles):
            print("      ", ligne, flush=True)
    vus = [c for c in cibles if any(c in l for l in lignes.rows)]
    print(f"    vues dans le log : {vus}", flush=True)
    return r


# P10 : mode B sur deux tables — une ligne `query=` par table.
exp("10", ['query=HR.REGIONS:"WHERE 1=2"',
           'query=HR.COUNTRIES:"WHERE 1=2"'],
    ["REGIONS", "COUNTRIES"])

# P11 : mode A — une seule clause, deux noms dans le `IN`.
exp("11", ['exclude=TABLE:"IN (\'EMPLOYEES\', \'COUNTRIES\')"'],
    ["EMPLOYEES", "COUNTRIES", "DEPARTMENTS"])

noms = [f"osdp{n}.{ext}" for n in ("10", "11") for ext in ("dmp", "log")]
print("nettoyage rc =", remote(["rm", "-f"] + [f"{DIRPATH}/{n}" for n in noms]).rc,
      flush=True)
print("tables maitres restantes :", p.source_adapter.query(
    "select table_name from dba_tables where owner='HR' "
    "and (table_name like 'OSDP%' or table_name like 'SYS_EXPORT%') order by 1"),
    flush=True)
print("fichiers restants :", remote(["ls", DIRPATH]).rows, flush=True)
