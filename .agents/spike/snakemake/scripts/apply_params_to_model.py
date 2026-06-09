"""Snakemake wrapper for apply_params."""
import json
import shutil
from pathlib import Path

template = Path(snakemake.input.template)  # noqa: F821
out_dir = Path(snakemake.output.out_dir)  # noqa: F821
out_dir.mkdir(parents=True, exist_ok=True)
params = snakemake.params.params_dict  # noqa: F821

# Stub: copy the template into the per-sample dir, attach the params.
if template.is_dir():
    for child in template.iterdir():
        dest = out_dir / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)
else:
    shutil.copy2(template, out_dir / template.name)

(out_dir / "params.json").write_text(json.dumps(params, indent=2))
print(f"applied params to {out_dir}")
