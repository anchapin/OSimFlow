from pathlib import Path
import yaml

example = Path("example_package")
example.mkdir(exist_ok=True)
(example / "model.osm").write_text("")
(example / "workflow.osw").write_text("")
