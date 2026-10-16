"""Sondes de combinaison : EXCLUDE + QUERY (modes A et B), INCLUDE + QUERY."""
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


def journal(nom):
    res = remote(["grep", "-n", "-E", "ORA-|UDE-|LRM-", f"{DIRPATH}/osdp{nom}.log"])
    return res.rows[:8] if res.rc == 0 else remote(
        ["tail", "-8", f"{DIRPATH}/osdp{nom}.log"]).rows[:8]


def exp(nom, extra, parallel=1):
    lines = [
        f'userid="{side.effective_connect()}"',
        'schemas="HR"',
        f'directory="{side.directory}"',
        f'logfile="osdp{nom}.log"',
        'content="ALL"',
        'compression="ALL"',
        f'job_name="OSDP{nom}"',
        (f'dumpfile="osdp{nom}-%d.dmp"' if parallel > 1 else f'dumpfile="osdp{nom}.dmp"'),
    ]
    if parallel > 1:
        lines.append(f"parallel={parallel}")
    lines.extend(extra)
    print(f"--- P{nom} : {extra} (parallel={parallel})", flush=True)
    r = pump.run("expdp", lines, job_name=f"OSDP{nom}", timeout=900)
    print(f"    rc={r.rc} erreurs={r.error_codes}", flush=True)
    print("    journal :", journal(nom), flush=True)
    l = remote(["grep", "exported", f"{DIRPATH}/osdp{nom}.log"])
    print("    REGIONS/COUNTRIES :",
          [x for x in l.rows if "REGIONS" in x or "COUNTRIES" in x], flush=True)
    return r


# P8 : INCLUDE + QUERY (le mode B ajoute un `query=`)
exp("8", ["include=TABLE", 'query=HR.REGIONS:"WHERE 1=2"'])
# P9 : EXCLUDE + QUERY (mode A + mode B, ou EXCLUDE de configuration + mode B)
exp("9", ['exclude=TABLE:"IN (\'EMPLOYEES\')"',
          'query=HR.REGIONS:"WHERE 1=2"'])

noms = [f"osdp{n}.{ext}" for n in ("8", "9") for ext in ("dmp", "log")]
print("nettoyage rc =", remote(["rm", "-f"] + [f"{DIRPATH}/{n}" for n in noms]).rc,
      flush=True)
print("tables maitres restantes :", p.source_adapter.query(
    "select table_name from dba_tables where owner='HR' "
    "and (table_name like 'OSDP%' or table_name like 'SYS_EXPORT%') order by 1"),
    flush=True)
