# Shim: moved to scripts\tests after cleanup.
from pathlib import Path
import runpy
import sys
target = Path(__file__).resolve().parent / "tests" / "Smoke-Test-AcGym.py"
sys.argv[0] = str(target)
runpy.run_path(str(target), run_name="__main__")
