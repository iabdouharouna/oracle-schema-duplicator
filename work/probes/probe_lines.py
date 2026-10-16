"""Sonde 5 : la limite est-elle la ligne ou le volume total ?"""
import sys
sys.path.insert(0, "src")
from pathlib import Path
from osd import config
from osd.state import State
from osd.stages.pipeline import Pipeline

cfg = config.load(Path("config/config.conf"))
p = Pipeline(cfg=cfg, state=State(run_id="probe5"), run_id="probe5")
ad = p.source_adapter

def essai(label, sql):
    try:
        val = ad.scalar(sql)
        print(f"{label} ({len(sql)} octets) : OK -> {val}", flush=True)
    except BaseException as exc:
        print(f"{label} ({len(sql)} octets) : FAIL {type(exc).__name__} "
              f"{getattr(exc, 'message', exc)}", flush=True)

# a) une ligne de 60 Ko (connue pour echouer)
essai("uneLigne60k", "select 1 from dual " + "--" + "x" * 60000)
# b) le meme volume, decoupe en lignes de 1000 octets
lignes = []
for _ in range(60):
    lignes.append("--" + "x" * 997)
essai("multiLigne60k", "select 1 from dual\n" + "\n".join(lignes))
# c) 200 Ko decoupes en lignes de 1000 octets
lignes = []
for _ in range(200):
    lignes.append("--" + "x" * 997)
essai("multiLigne200k", "select 1 from dual\n" + "\n".join(lignes))
# d) une ligne de 200 Ko
essai("uneLigne200k", "select 1 from dual " + "--" + "x" * 200000)
print("fin", flush=True)
