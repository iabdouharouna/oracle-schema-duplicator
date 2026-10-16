import sys
sys.path.insert(0, "tests/unit")
sys.path.insert(0, "src")
import test_pipeline as tp

class T(tp.TestFiltrageDesTables):
    def runTest(self): pass

t = T(methodName="runTest")
t.setUp()
s = t.nouveau(config={"EXCLUDE_TABLES": "EMPLOYEES",
                      "EXCLUDE_TABLES_DATA": "SAL_HISTORY"}, source=t.source())
print("code", s.aller())
for label, runner in (("SOURCE", s.source_runner), ("CIBLE", s.target_runner)):
    for n, script in enumerate(runner.scripts_de("datapump")):
        if "osd_parcontent='" not in script:
            continue
        brut = script.split("osd_parcontent='", 1)[1]
        fin = brut.find("\n'\n")
        contenu = brut[:fin].replace("'\\''", "'")
        print("=== %s #%d ===" % (label, n))
        print(contenu)
