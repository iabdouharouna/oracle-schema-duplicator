import sys
sys.path.insert(0, "..")
sys.path.insert(0, "../src")
import test_pipeline as tp
class T(tp.TestFiltrageDesTables):
    def runTest(self): pass
t=T(methodName="runTest")
t.setUp()
s=t.nouveau(config={"EXCLUDE_TABLES":"EMPLOYEES"}, source=t.source())
from osd import exit_codes as ec
print(s.aller()==ec.SUCCESS)
print(len(s.source_runner.scripts_de("datapump")))
