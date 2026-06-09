"""Diagnostic: print cache hash values to find the divergence."""
import hashlib
import os
import sys
import subprocess
from pathlib import Path

VENV = Path("/home/alex/Projects/OSimFlow/.agents/spike/.venv")

cmd = [
    str(VENV / "bin" / "python"), "-c",
    "import sys; sys.path.insert(0, '/home/alex/Projects/OSimFlow/.agents/spike/custom_python');"
    "from osimflow.cache import sha256_of_files;"
    "from pathlib import Path;"
    "import inspect, os; os.environ['OSIMFLOW_PROJECT_ROOT']='/home/alex/Projects/OSimFlow';"
    "from osimflow import work;"
    "files = sorted(Path('/home/alex/Projects/OSimFlow/bin').glob('*.py'));"
    "wf = Path(inspect.getfile(work));"
    "print('BIN_HASH:', sha256_of_files(files));"
    "print('WORK_HASH:', sha256_of_files([wf]));"
    "print('files:', [f.name for f in files]);"
]

# Run twice; outputs should be identical.
for i in range(2):
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(f"--- run {i+1} ---")
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr[:200])
