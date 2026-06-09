import sys

work_py = open("osimflow/work.py").read()

new_work_py = work_py.replace(
    'def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:',
    """def generate_lhs(variables_yml: Path, n_samples: int, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    samples_json = out / "samples.json"
    result = subprocess.run(
        [
            sys.executable, str(BIN / "generate_lhs.py"),
            "--variables_yml", str(variables_yml),
            "--n_samples", str(n_samples),
            "--out", str(samples_json),
        ],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("generate_lhs failed: %s", result.stderr)
        raise RuntimeError("generate_lhs failed")
    return samples_json

def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:"""
)

with open("osimflow/work.py", "w") as f:
    f.write(new_work_py)
