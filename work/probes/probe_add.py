"""Sonde : EXCLUDE_TABLES + EXCLUDE_STANDALONE_TABLES s'additionnent-ils ?

Read-only. On demande l'exclusion totale d'une table non isolee
(CR_TRANSACTION, qui porte des FK) et la regle des isolees en meme temps.
Le plan doit porter les deux effets, sans que l'un efface l'autre.
"""
import sys, time
from pathlib import Path

sys.path.insert(0, "src")
from osd import config
from osd.checks.table_filter import plan_table_filter
from osd.stages.pipeline import Pipeline
from osd.state import State

cfg = config.load(Path("config/config.conf"))
schema = str(cfg.get("SOURCE_SCHEMA"))
ad = Pipeline(cfg=cfg, state=State(run_id="probe"), run_id="probe").source_adapter

t0 = time.time()
plan = plan_table_filter(
    ad, schema,
    tables=["CR_TRANSACTION"],            # mode A (exclusion totale)
    tables_data=["MEGA_DF"],              # mode B (liste ecrite)
    standalone=True,                      # mode B (regle isolee)
)
print(f"plan en {time.time()-t0:.1f}s")
print(f"excluded (mode A)   : {plan.excluded}")
print(f"data_only (mode B)  : {len(plan.data_only)} tables "
      f"(dont MEGA_DF={'MEGA_DF' in plan.data_only}, "
      f"CR_TRANSACTION={'CR_TRANSACTION' in plan.data_only})")
print(f"standalone          : {len(plan.standalone)} tables")
print(f"overlap             : {plan.overlap}")
print(f"dependents retires  : {len(plan.dependents)}")
print(f"unmatched           : {plan.unmatched}")
print(f"clause exclude      : {plan.exclude_clause()}")
print(f"lignes query        : {len(plan.query_lines())}")
print(f"  MEGA_DF present   : {any('MEGA_DF' in l for l in plan.query_lines())}")
print(f"  CR_TRANSACTION    : {any('CR_TRANSACTION' in l for l in plan.query_lines())}")

# Verifie que l'union est bien data_only = {MEGA_DF} U isolees
print(f"\nMEGA_DF isolee ?     : {any(t == 'MEGA_DF' for t in plan.standalone)}")
