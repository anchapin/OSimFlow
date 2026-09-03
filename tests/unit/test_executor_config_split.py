"""Tests for the per-executor config/argument split (issue #1575).

Covers the three acceptance guarantees of the refactor:

1. **Zero CLI behavior change** — the ``run`` subparser built from the
   per-executor ``add_arguments`` hooks matches, flag for flag and
   attribute for attribute (dest / default / type / choices / action /
   nargs / metavar / help / required), the pre-refactor argparse tree
   captured in ``fixtures/run_cli_flags_pre1575.json`` (snapshot taken
   from the monolithic ``osimflow/__main__.py`` at ``origin/main``
   before the split).
2. **``load_config`` behavior unchanged** — a representative CLI-arg
   dict (including executor keys from every substrate) still parses
   into the composed ``CampaignConfig`` sections.
3. **Plug-in discoverability** — a dummy third-party executor can
   register an ``add_arguments`` hook (directly or via
   ``ExecutorRegistry.register_arguments``) and its flag shows up on
   the parser ``osimflow run`` uses.
"""

import argparse
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from osimflow.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "tests" / "unit" / "fixtures" / "run_cli_flags_pre1575.json"

#: The ten built-in executors that must own a registered argument hook.
BUILTIN_EXECUTORS = {
    "aws_batch",
    "azure_batch",
    "dask_jobqueue",
    "docker_swarm",
    "google_batch",
    "kubernetes",
    "local",
    "nomad",
    "pbs",
    "slurm",
}


def _build_run_parser() -> argparse.ArgumentParser:
    """Build the run subparser exactly the way ``osimflow run`` does."""
    from osimflow.__main__ import _add_run_args

    parser = argparse.ArgumentParser()
    run = parser.add_subparsers().add_parser("run")
    _add_run_args(run)
    return run


def _normalize_default(default: object) -> object:
    if default is None:
        return None
    if isinstance(default, (list, tuple)):
        return [str(item) for item in default]
    return str(default)


def _action_specs(run: argparse.ArgumentParser) -> dict[str, dict[str, object]]:
    """Per-flag attribute specs for every registered ``--flag``.

    Uses ``option_strings[0]`` (the *registered* spelling) so
    ``BooleanOptionalAction`` flags count once under their positive
    form while a literal ``--no-tui`` still counts as itself.
    """
    specs: dict[str, dict[str, object]] = {}
    for action in run._actions:
        if not action.option_strings:
            continue
        flag = action.option_strings[0]
        if not flag.startswith("--") or flag == "--help":
            continue
        specs[flag] = {
            "dest": action.dest,
            "option_strings": list(action.option_strings),
            "default": _normalize_default(action.default),
            "type": getattr(action.type, "__name__", None) if action.type else None,
            "choices": [str(choice) for choice in action.choices] if action.choices else None,
            "action": type(action).__name__,
            "nargs": str(action.nargs) if action.nargs is not None else None,
            "const": str(action.const) if action.const is not None else None,
            "metavar": str(action.metavar) if action.metavar is not None else None,
            "help": action.help,
            "required": bool(action.required),
        }
    return specs


class TestFlagSurfaceParity:
    """Issue #1575 acceptance: hooks reproduce the old flat flag list."""

    def test_run_flag_surface_matches_pre_refactor_snapshot(self) -> None:
        specs = _action_specs(_build_run_parser())
        expected: dict[str, dict[str, object]] = json.loads(SNAPSHOT_PATH.read_text())
        assert set(specs) == set(expected), (
            f"flags only in current parser: {sorted(set(specs) - set(expected))}; "
            f"flags only in pre-#1575 snapshot: {sorted(set(expected) - set(specs))}"
        )
        mismatched = [flag for flag in expected if specs[flag] != expected[flag]]
        assert not mismatched, (
            "flags whose dest/default/type/choices/action/help drifted from the "
            f"pre-#1575 argparse tree: {sorted(mismatched)}"
        )

    def test_every_executor_flag_comes_from_a_registered_hook(self) -> None:
        """All 68 executor-owned flags must be registered by the hooks
        (i.e. present on a parser built from ``add_executor_arguments``
        alone), while campaign-level flags must NOT be."""
        from osimflow.executor_configs import add_executor_arguments

        parser = argparse.ArgumentParser()
        run = parser.add_subparsers().add_parser("run")
        add_executor_arguments(run)
        hook_flags = set(_action_specs(run))
        assert len(hook_flags) == 68
        # Representative flags from every executor module.
        assert {
            "--max-workers",
            "--slurm-real",
            "--slurm-cost-per-node-hour",
            "--aws-batch-queue",
            "--ecr-repository",
            "--aws-batch-on-demand-price",
            "--nomad-dispatch-job-id",
            "--azure-max-retries",
            "--google-batch-region",
            "--kubernetes-queue-name",
            "--pbs-real",
            "--dask-walltime",
            "--docker-swarm-image",
        } <= hook_flags
        # Campaign-level flags stay central in __main__.py.
        assert (
            not {
                "--executor",
                "--task-queue",
                "--dask-scheduler-address",
                "--shard-count",
                "--input_variables",
            }
            & hook_flags
        )

    def test_all_ten_builtin_hooks_registered(self) -> None:
        from osimflow.executor_configs import iter_executor_argument_hooks

        assert set(name for name, _hook in iter_executor_argument_hooks()) == BUILTIN_EXECUTORS

    def test_boolean_optional_action_positive_spelling_is_primary(self) -> None:
        """The registered spelling of BooleanOptionalAction flags is the
        positive form (mirrors the pre-refactor ``add_argument`` calls)."""
        run = _build_run_parser()
        specs = _action_specs(run)
        assert "--nomad-tls-verify" in specs
        assert "--nomad-tls" in specs
        assert "--no-nomad-tls-verify" not in specs
        # --no-tui is a plain store_true flag, not a negative spelling.
        assert "--no-tui" in specs


class TestPluginHookRegistration:
    """Issue #1575 acceptance: entry-point plug-ins get a config home."""

    @staticmethod
    def _dummy_hook(parser_group: argparse.ArgumentParser) -> None:
        parser_group.add_argument(
            "--dummy-plugin-token",
            default=None,
            help="dummy plug-in flag (issue #1575 test)",
        )

    def test_register_executor_arguments_makes_flag_parseable(self) -> None:
        from osimflow.executor_configs import (
            add_executor_arguments,
            register_executor_arguments,
        )

        register_executor_arguments("dummy_plugin", self._dummy_hook)
        try:
            parser = argparse.ArgumentParser()
            run = parser.add_subparsers().add_parser("run")
            add_executor_arguments(run)
            namespace = run.parse_args(["--dummy-plugin-token", "abc123"])
            assert namespace.dummy_plugin_token == "abc123"
        finally:
            from osimflow.executor_configs.base import _EXECUTOR_ARGUMENT_HOOKS

            _EXECUTOR_ARGUMENT_HOOKS.pop("dummy_plugin", None)

    def test_executor_registry_register_arguments_delegates(self) -> None:
        from osimflow.executors import ExecutorRegistry

        ExecutorRegistry.register_arguments("dummy_plugin", self._dummy_hook)
        try:
            hooks = dict(ExecutorRegistry.iter_argument_hooks())
            assert "dummy_plugin" in hooks
            assert hooks["dummy_plugin"] is self._dummy_hook
        finally:
            from osimflow.executor_configs.base import _EXECUTOR_ARGUMENT_HOOKS

            _EXECUTOR_ARGUMENT_HOOKS.pop("dummy_plugin", None)

    def test_discover_plugins_auto_registers_add_arguments_staticmethod(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plug-in class exposing an ``add_arguments`` staticmethod gets
        its hook auto-registered under its entry-point name."""
        from osimflow.executors import ExecutorRegistry
        from osimflow.executors.base import BaseExecutor

        class _PluginExecutor(BaseExecutor):
            add_arguments = staticmethod(self._dummy_hook)

            def submit(
                self,
                fn: Callable[[], object],
                description: str = "",
                step_name: str = "",
                sample_id: str | None = None,
                cpus: int = 1,
                memory_mb: int = 1024,
                time_min: int = 10,
            ) -> object:  # pragma: no cover - shape only
                raise NotImplementedError

        class _FakeEntryPoint:
            name = "dummy_plugin"
            value = "dummy_pkg:DummyExecutor"

            def load(self) -> type[BaseExecutor]:
                return _PluginExecutor

        def _fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
            return [_FakeEntryPoint()] if group == "osimflow.executors" else []

        monkeypatch.setattr("osimflow.executors.entry_points", _fake_entry_points)
        try:
            assert ExecutorRegistry.discover_plugins() == 1
            hooks = dict(ExecutorRegistry.iter_argument_hooks())
            assert "dummy_plugin" in hooks
        finally:
            from osimflow.executor_configs.base import _EXECUTOR_ARGUMENT_HOOKS
            from osimflow.executors.base import _EXECUTOR_REGISTRY

            _EXECUTOR_ARGUMENT_HOOKS.pop("dummy_plugin", None)
            _EXECUTOR_REGISTRY.pop("dummy_plugin", None)


class TestConfigComposition:
    """``CampaignConfig`` composition and re-exports stay intact."""

    def test_config_reexports_executor_configs(self) -> None:
        import osimflow.config as config_module
        from osimflow.executor_configs import (
            AWSBatchConfig,
            AzureBatchConfig,
            GoogleBatchConfig,
            LocalConfig,
            NomadConfig,
            SlurmConfig,
        )

        assert config_module.SlurmConfig is SlurmConfig
        assert config_module.AWSBatchConfig is AWSBatchConfig
        assert config_module.AzureBatchConfig is AzureBatchConfig
        assert config_module.GoogleBatchConfig is GoogleBatchConfig
        assert config_module.NomadConfig is NomadConfig
        assert config_module.LocalConfig is LocalConfig

    def test_load_config_parses_representative_executor_args(self, tmp_path: Path) -> None:
        """A CLI-shaped dict covering every executor substrate still
        parses through ``load_config`` into the composed sections."""
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "wall_r",
                            "distribution": "uniform",
                            "min": 1.0,
                            "max": 10.0,
                        }
                    ]
                }
            )
        )
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text("{}")
        outdir = tmp_path / "out"
        outdir.mkdir()

        args: dict[str, object] = {
            "input_variables": str(variables_yml),
            "template_sim_package": str(template),
            "n_samples": 4,
            "outdir": str(outdir),
            "openstudio_version": "3.11.0",
            # local
            "max_workers": 5,
            # slurm
            "slurm_qos": "high",
            "slurm_constraint": "gpu",
            "slurm_gres": "gpu:1",
            # aws_batch
            "aws_batch_max_spot_price_usd": 0.05,
            "aws_batch_fallback_to_on_demand": True,
            "aws_batch_max_retries": 5,
            "aws_batch_submit_rps": 100.0,
            "ecr_repository": "1234.dkr.ecr.us-east-1.amazonaws.com/osimflow",
            # azure_batch
            "azure_batch_account_name": "acct",
            "azure_batch_pool_id": "pool-1",
            "azure_use_spot": True,
            # google_batch
            "google_batch_project_id": "proj",
            "google_batch_region": "us-west1",
            # nomad
            "nomad_dispatch_policy": "force_dispatch",
            "nomad_poll_interval_s": 2.5,
            "nomad_tls": True,
            # kubernetes
            "kubernetes_backoff_limit": 2,
            "kubernetes_queue_name": "team-a-cpu",
        }
        cfg = load_config(args)

        assert cfg.slurm is not None and cfg.slurm.qos == "high"
        assert cfg.slurm.gres == "gpu:1"
        assert cfg.aws_batch is not None and cfg.aws_batch.max_spot_price_usd == 0.05
        assert cfg.aws_batch.submit_rps == 100.0
        assert cfg.azure_batch is not None and cfg.azure_batch.account_name == "acct"
        assert cfg.azure_batch.use_spot is True
        assert cfg.google_batch is not None and cfg.google_batch.region == "us-west1"
        assert cfg.nomad is not None and cfg.nomad.dispatch_policy == "force_dispatch"
        assert cfg.nomad.poll_interval_s == 2.5
        assert cfg.nomad.tls is True
        assert cfg.kubernetes_backoff_limit == 2
        assert cfg.kubernetes_queue_name == "team-a-cpu"
        assert cfg.dag.ecr_repository == "1234.dkr.ecr.us-east-1.amazonaws.com/osimflow"
        # Legacy flat delegation keeps working (issue #724 contract).
        assert cfg.slurm_qos == "high"
        assert cfg.aws_batch_max_retries == 5
