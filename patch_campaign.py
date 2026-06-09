import re

with open("osimflow/campaign.py", "r") as f:
    content = f.read()

# Add import for generate_lhs to campaign.py
content = content.replace("from osimflow.work import (", "from osimflow.work import (\n    generate_lhs,")

# Replace step_generate_lhs
old_step = '''    def step_generate_lhs(self) -> list[dict]:
        """Single-shot: read variables.yml, produce N parameter sets.

        The MVP uses an in-process LHS implementation so the campaign
        is self-contained. The LHS algorithm in `bin/generate_lhs.py`
        will replace this in-process version once implemented; the
        output schema is identical.
        """
        t0 = time.time()
        inputs_hash = sha256_of_files([self.cfg.input_variables])
        key = CacheKey(
            step="GENERATE_LHS_SAMPLES",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["bin"],
            container_digest=CONTAINER_PY,
        )
        cached = self.cache.lookup(key)
        if cached:
            samples = json.loads(cached.read_text())["samples"]
            self.trace.step_finished(
                "GENERATE_LHS_SAMPLES", cache="HIT",
                elapsed_s=time.time() - t0, exit_code=0,
            )
            return samples

        with self.cfg.input_variables.open() as f:
            variables = yaml.safe_load(f)["variables"]
        rng = random.Random(0)  # deterministic for cache stability
        samples = []
        for i in range(self.cfg.n_samples):
            values = {}
            for v in variables:
                if v["distribution"] == "uniform":
                    values[v["name"]] = v["min"] + rng.random() * (v["max"] - v["min"])
                elif v["distribution"] == "lognormal":
                    # lognormal via Box-Muller
                    u1 = max(rng.random(), 1e-9)
                    u2 = rng.random()
                    z = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
                    values[v["name"]] = math.exp(v["mean"] + v["sigma"] * z)
                else:
                    raise NotImplementedError(
                        f"distribution {v['distribution']!r} not in MVP yet")
            samples.append({"sample_id": f"{i+1:04d}", "values": values})
        out_json = self.cfg.work_dir / "samples.json"
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps({
            "n_samples": len(samples),
            "variables": variables,
            "samples": samples,
        }, indent=2))
        self.cache.store(key, out_json, exit_code=0)
        self.trace.step_finished(
            "GENERATE_LHS_SAMPLES", cache="MISS",
            elapsed_s=time.time() - t0, exit_code=0,
        )
        return samples'''

new_step = '''    def step_generate_lhs(self) -> list[dict]:
        """Single-shot: read variables.yml, produce N parameter sets.

        Calls `bin/generate_lhs.py` via the executor.
        """
        t0 = time.time()
        inputs_hash = sha256_of_files([self.cfg.input_variables])
        key = CacheKey(
            step="GENERATE_LHS_SAMPLES",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["bin"],
            container_digest=CONTAINER_PY,
        )
        cached = self.cache.lookup(key)
        if cached:
            samples = json.loads(cached.read_text())["samples"]
            self.trace.step_finished(
                "GENERATE_LHS_SAMPLES", cache="HIT",
                elapsed_s=time.time() - t0, exit_code=0,
            )
            return samples

        out_dir = self.cfg.work_dir / "lhs"
        handle = self.executor.submit(
            generate_lhs,
            self.cfg.input_variables, self.cfg.n_samples, out_dir,
            name="generate_lhs",
            cpus=1, memory_mb=1024, time_min=5,
            container=CONTAINER_PY,
        )
        try:
            result_path = handle.result(timeout=120)
            self.cache.store(key, Path(result_path), exit_code=0)
            samples = json.loads(Path(result_path).read_text())["samples"]
            self.trace.step_finished(
                "GENERATE_LHS_SAMPLES", cache="MISS",
                elapsed_s=time.time() - t0, exit_code=0,
            )
            return samples
        except Exception as e:
            log.error("GENERATE_LHS_SAMPLES failed: %s", e)
            self.trace.step_finished(
                "GENERATE_LHS_SAMPLES", cache="MISS",
                elapsed_s=time.time() - t0, exit_code=1,
            )
            raise'''

if old_step in content:
    content = content.replace(old_step, new_step)
    with open("osimflow/campaign.py", "w") as f:
        f.write(content)
    print("Patched campaign.py successfully")
else:
    print("Could not find the old step_generate_lhs to replace")
