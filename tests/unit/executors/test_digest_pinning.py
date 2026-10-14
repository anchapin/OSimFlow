"""Digest-pinned image execution across container substrates (issue #1536).

The cache key pins content via the container digest (issues #1023/#1218),
but the executed image used to resolve by mutable tag on Kubernetes
(``OSIMFLOW_CONTAINER`` env), Docker Swarm (``image = container or
self.image``), and Nomad (``OSIMFLOW_OPENSTUDIO_CONTAINER_IMAGE``) — and
``Campaign`` submitted the tag while the cache key described a digest.
A re-published tag therefore changed what ran while a warm cache replayed
results as if the old digest had produced them.

These tests pin the #1536 contract: when a digest is known, every
submitted job/pod/task references ``<repo>@sha256:<hex>``; unresolved
digests fail loudly for container substrates; cosign triangulates tag
refs before verification and the receipt records the verified digest.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.cache import ImageDigestUnavailableError, digest_pinned_image_ref
from osimflow.campaign import CONTAINER_SUBSTRATE_EXECUTORS, Campaign
from osimflow.config import CampaignConfig
from osimflow.cosign import (
    CosignVerificationError,
    triangulate_image_ref,
    write_cosign_receipt,
)
from osimflow.executors.base import Handle

HEX = "a" * 64
PINNED = f"docker.io/nrel/openstudio@sha256:{HEX}"


class TestDigestPinnedImageRef:
    @pytest.mark.parametrize(
        ("label", "digest", "expected"),
        [
            # cache-key form from _container_digest_for
            (
                "docker.io/nrel/openstudio:3.11.0",
                f"docker.io/nrel/openstudio:3.11.0@docker.io/nrel/openstudio@sha256:{HEX}",
                PINNED,
            ),
            # direct label@digest form
            (
                "docker.io/nrel/openstudio:3.11.0",
                f"docker.io/nrel/openstudio:3.11.0@sha256:{HEX}",
                PINNED,
            ),
            # repo@digest form
            ("docker.io/nrel/openstudio:3.11.0", f"docker.io/nrel/openstudio@sha256:{HEX}", PINNED),
            # bare digest — label contributes the repo (tag stripped)
            ("docker.io/nrel/openstudio:3.11.0", f"sha256:{HEX}", PINNED),
            # registry with port survives
            (
                "docker.io/nrel/openstudio:3.11.0",
                f"registry:5000/os@sha256:{HEX}",
                f"registry:5000/os@sha256:{HEX}",
            ),
            # unresolved sentinel / missing / garbage
            (
                "docker.io/nrel/openstudio:3.11.0",
                "docker.io/nrel/openstudio:3.11.0@unresolved",
                None,
            ),
            ("docker.io/nrel/openstudio:3.11.0", None, None),
            ("docker.io/nrel/openstudio:3.11.0", "garbage", None),
        ],
    )
    def test_forms(self, label: str, digest: str | None, expected: str | None) -> None:
        assert digest_pinned_image_ref(label, digest) == expected


class _NamedStubExecutor:
    """Executor stub carrying only the name the run() gate reads."""

    name = "kubernetes"

    def __getattr__(self, item: str) -> Any:
        # Campaign init probes several optional executor attributes;
        # answer None for everything except ``name``.
        return None


class TestCampaignDigestPinning:
    def _cfg(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, **overrides: Any
    ) -> CampaignConfig:
        defaults: dict[str, Any] = {
            "input_variables": variables_yml,
            "template_sim_package": template_pkg,
            "n_samples": 1,
            "outdir": outdir,
            "openstudio_version": "3.11.0",
            "dry_run": True,
        }
        defaults.update(overrides)
        return CampaignConfig(**defaults)

    def test_provided_digest_pins_submit_container(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = self._cfg(variables_yml, template_pkg, outdir, container_digest=PINNED)
        campaign = Campaign(cfg=cfg, executor=_NamedStubExecutor())
        assert campaign._os_digest_ref == PINNED
        kwargs = campaign._build_run_sim_submit_kwargs(
            {"stdout_log": Path("/dev/null"), "stderr_log": Path("/dev/null"), "out_dir": outdir},
            "0001",
            "3.11.0",
        )
        assert kwargs["container"] == PINNED, "submit must reference the digest-pinned ref"

    def test_container_substrates_fail_loudly_on_unresolved_digest(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the "unresolved" sentinel regardless of local docker state.
        import osimflow.campaign as campaign_mod

        monkeypatch.setattr(
            campaign_mod, "_container_digest_for", lambda label: f"{label}@unresolved"
        )
        cfg = self._cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=_NamedStubExecutor())
        with pytest.raises(RuntimeError, match=r"digest-pinned|--container-digest"):
            campaign.run()

    def test_container_substrate_set(self) -> None:
        assert CONTAINER_SUBSTRATE_EXECUTORS == {
            "aws_batch",
            "azure_batch",
            "docker_swarm",
            "google_batch",
            "kubernetes",
            "nomad",
        }


class TestKubernetesDigestPinning:
    def test_env_uses_pinned_ref_when_digest_known(self) -> None:
        from osimflow.executors.kubernetes_executor import KubernetesExecutor

        ex = KubernetesExecutor.__new__(KubernetesExecutor)
        ex._container_digest = f"docker.io/nrel/openstudio:3.11.0@{PINNED}"
        env = ex._build_environment(
            openstudio_version="3.11.0",
            container="docker.io/nrel/openstudio:3.11.0",
            task_payload=None,
        )
        entry = next(e for e in env if e["name"] == "OSIMFLOW_CONTAINER")
        assert entry["value"] == PINNED

    def test_env_falls_back_to_tag_when_unresolved(self) -> None:
        from osimflow.executors.kubernetes_executor import KubernetesExecutor

        ex = KubernetesExecutor.__new__(KubernetesExecutor)
        ex._container_digest = "docker.io/nrel/openstudio:3.11.0@unresolved"
        env = ex._build_environment(
            openstudio_version="3.11.0",
            container="docker.io/nrel/openstudio:3.11.0",
            task_payload=None,
        )
        entry = next(e for e in env if e["name"] == "OSIMFLOW_CONTAINER")
        assert entry["value"] == "docker.io/nrel/openstudio:3.11.0"


class TestSwarmDigestPinning:
    def test_service_image_pinned_when_digest_known(self) -> None:
        from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor

        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        captured: dict[str, Any] = {}

        class _FakeService:
            name = "svc-1"

        class _Services:
            def create(self, **kw: Any) -> _FakeService:  # noqa: ANN401
                captured["spec"] = kw
                return _FakeService()

        class _Client:
            services = _Services()

        ex._get_client = lambda: _Client()  # type: ignore[method-assign]
        ex._build_service_name = lambda name: f"osimflow-{name}"  # type: ignore[method-assign]
        ex.image = "nrel/openstudio:3.11.0"
        ex.network = None
        ex._submit_service(
            name="sim_0001",
            cpus=2,
            memory_mb=2048,
            time_min=60,
            openstudio_version="3.11.0",
            container="docker.io/nrel/openstudio:3.11.0",
            container_digest=f"docker.io/nrel/openstudio:3.11.0@{PINNED}",
        )
        assert captured["spec"]["image"] == PINNED


class TestNomadDigestPinning:
    def test_resolve_prefers_pinned_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.executors.nomad_executor import NomadExecutor

        monkeypatch.delenv("OSIMFLOW_NOMAD_PREFERRED_IMAGE", raising=False)
        monkeypatch.delenv("OSIMFLOW_OPENSTUDIO_CONTAINER_IMAGE", raising=False)
        image = NomadExecutor._resolve_nomad_image(
            container="docker.io/nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            container_digest=f"docker.io/nrel/openstudio:3.11.0@{PINNED}",
        )
        assert image == PINNED

    def test_resolve_falls_back_without_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.executors.nomad_executor import NomadExecutor

        monkeypatch.delenv("OSIMFLOW_NOMAD_PREFERRED_IMAGE", raising=False)
        monkeypatch.delenv("OSIMFLOW_OPENSTUDIO_CONTAINER_IMAGE", raising=False)
        image = NomadExecutor._resolve_nomad_image(
            container="docker.io/nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            container_digest=None,
        )
        assert image == "docker.io/nrel/openstudio:3.11.0"


class TestCosignTriangulate:
    def test_already_pinned_passthrough(self) -> None:
        assert triangulate_image_ref(PINNED) == PINNED

    def test_tag_ref_triangulated(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(returncode=0, stdout=PINNED + "\n", stderr="")
        with (
            patch("osimflow.cosign.subprocess.run", return_value=fake) as mock_run,
            patch("osimflow.cosign.shutil.which", return_value="/usr/bin/cosign"),
        ):
            result = triangulate_image_ref("docker.io/nrel/openstudio:3.11.0")
        assert result == PINNED
        assert mock_run.call_args.args[0][1:] == ["triangulate", "docker.io/nrel/openstudio:3.11.0"]

    def test_triangulate_failure_raises(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(returncode=1, stdout="", stderr="not found")
        with (
            patch("osimflow.cosign.subprocess.run", return_value=fake),
            patch("osimflow.cosign.shutil.which", return_value="/usr/bin/cosign"),
        ):
            with pytest.raises(CosignVerificationError, match="triangulate FAILED"):
                triangulate_image_ref("docker.io/nrel/openstudio:3.11.0")

    def test_receipt_contains_digest_fields(self, tmp_path: Path) -> None:
        path = write_cosign_receipt(
            tmp_path,
            image_ref=PINNED,
            certificate_identity="https://github.com/NREL/OpenStudio/.github/workflows/release.yml@refs/heads/main",
            certificate_oidc_issuer="https://token.actions.githubusercontent.com",
            verified_digest=f"sha256:{HEX}",
            triangulated_from_tag="docker.io/nrel/openstudio:3.11.0",
        )
        receipt = json.loads(path.read_text())
        assert receipt["verified_digest"] == f"sha256:{HEX}"
        assert receipt["triangulated_from_tag"] == "docker.io/nrel/openstudio:3.11.0"
        assert receipt["verified"] is True


class TestAwsBatchJobDefinitionDigestResolution:
    """Issue #2027: AWS Batch resolves/validates job-definition digest without local Docker."""

    @staticmethod
    def _cfg(
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
        **overrides: Any,
    ) -> CampaignConfig:
        defaults: dict[str, Any] = {
            "input_variables": variables_yml,
            "template_sim_package": template_pkg,
            "n_samples": 1,
            "outdir": outdir,
            "openstudio_version": "3.11.0",
        }
        defaults.update(overrides)
        return CampaignConfig(**defaults)

    @staticmethod
    def _make_batch_executor(job_def_image: str) -> tuple[Any, MagicMock]:
        from osimflow.executors.aws_batch_executor import AWSBatchExecutor

        ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        ex.job_queue = "osimflow-batch-queue"
        ex.job_definition = "osimflow-openstudio-job-def"
        ex.region_name = "us-east-1"
        ex.poll_interval_s = 0.01
        ex.max_retries = 0
        ex.use_spot = False
        ex.spot_price_pct = 100
        ex.on_demand_queue = None
        ex.ecr_repository = None
        ex.s3_artifact_bucket = None
        ex.s3_artifact_prefix = "osimflow"
        ex.s3_artifact_endpoint = None
        ex.allow_insecure_storage_endpoint = False
        ex.submit_rate_per_sec = 0.0
        ex.fanout_chunk_size = 0
        ex.allow_long_lived_credentials = True
        ex._container_digest = None
        ex._fargate = False
        ex._payload_secret_arn = None

        fake_client = MagicMock()
        fake_client.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "osimflow-openstudio-job-def",
                    "revision": 3,
                    "status": "ACTIVE",
                    "containerProperties": {"image": job_def_image},
                }
            ]
        }
        ex._client = fake_client
        return ex, fake_client

    def test_no_local_docker_digest_pinned_job_def_succeeds_and_records_digest(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """No local Docker + job definition specifies nrel/openstudio@sha256:... -> succeeds and records digest."""
        import sqlite3

        assert "aws_batch" in CONTAINER_SUBSTRATE_EXECUTORS
        pinned_job_def_image = f"nrel/openstudio@sha256:{HEX}"
        ex, fake_client = self._make_batch_executor(pinned_job_def_image)

        def _exec_inline(
            fn: Any,
            *args: Any,
            name: str | None = None,
            cpus: float = 1,
            memory_mb: int = 2048,
            time_min: int = 30,
            openstudio_version: str | None = None,
            container: str | None = None,
            container_digest: str | None = None,
            result_hint: Any = None,
            transport: Any = None,
            **kwargs: Any,
        ) -> Handle:
            fut: Future[Any] = Future()
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)
            return Handle(job_id="batch-stub", _future=fut)

        ex.submit = _exec_inline  # type: ignore[method-assign]

        with patch(
            "osimflow.cache._resolve_image_digest",
            side_effect=ImageDigestUnavailableError("docker unavailable"),
        ):
            campaign = Campaign(
                self._cfg(variables_yml, template_pkg, outdir, container_digest=None),
                ex,
            )
            assert campaign._os_digest_ref == pinned_job_def_image
            assert pinned_job_def_image in campaign._os_container_digest
            result = campaign.run()

        assert (outdir / "aggregated_results.csv").is_file()
        assert "samples" in result
        fake_client.describe_job_definitions.assert_called_once()

        # Verify digest is recorded in run.json, campaign_meta.json, provenance.json, and SQLite cache.
        run_json = json.loads((outdir / "run.json").read_text())
        assert run_json["config"]["container_digest"] == pinned_job_def_image
        meta_json = json.loads((outdir / "campaign_meta.json").read_text())
        assert meta_json["container_digest"] == pinned_job_def_image
        prov_json = json.loads((outdir / "provenance.json").read_text())
        assert prov_json["container_digest"] == pinned_job_def_image

        with sqlite3.connect(outdir / "work" / "cache.sqlite") as conn:
            rows = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT container_digest FROM cache_entries WHERE step = 'RUN_OPENSTUDIO_SIM'"
                ).fetchall()
            ]
        assert rows == [f"docker.io/nrel/openstudio:3.11.0@{pinned_job_def_image}"]

    def test_no_local_docker_mutable_job_def_without_container_digest_fails_before_submission(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """No local Docker + mutable/unresolvable job definition image without --container-digest -> fails before job submission."""
        ex, fake_client = self._make_batch_executor("nrel/openstudio:3.11.0")
        with patch(
            "osimflow.cache._resolve_image_digest",
            side_effect=ImageDigestUnavailableError("docker unavailable"),
        ):
            campaign = Campaign(
                self._cfg(variables_yml, template_pkg, outdir, container_digest=None),
                ex,
            )
            assert campaign._os_digest_ref is None
            with pytest.raises(
                RuntimeError,
                match=r"requires a digest-pinned container image.*containerProperties\.image",
            ):
                campaign.run()
        fake_client.submit_job.assert_not_called()

    def test_explicit_container_digest_mismatching_digest_pinned_job_def_fails_before_submission(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """Explicitly supplied --container-digest mismatching a digest-pinned job definition -> fails."""
        other_hex = "b" * 64
        ex, fake_client = self._make_batch_executor(f"nrel/openstudio@sha256:{HEX}")
        with patch(
            "osimflow.cache._resolve_image_digest",
            side_effect=ImageDigestUnavailableError("docker unavailable"),
        ):
            campaign = Campaign(
                self._cfg(
                    variables_yml,
                    template_pkg,
                    outdir,
                    container_digest=f"nrel/openstudio@sha256:{other_hex}",
                ),
                ex,
            )
            with pytest.raises(RuntimeError, match="does not match the image"):
                campaign.run()
        fake_client.submit_job.assert_not_called()

    def test_other_container_substrates_remain_gated(
        self,
        variables_yml: Path,
        template_pkg: Path,
        tmp_path: Path,
    ) -> None:
        """All other container substrates remain gated in CONTAINER_SUBSTRATE_EXECUTORS."""
        expected = {
            "aws_batch",
            "azure_batch",
            "google_batch",
            "kubernetes",
            "docker_swarm",
            "nomad",
        }
        assert CONTAINER_SUBSTRATE_EXECUTORS == expected
        for name in sorted(expected - {"aws_batch"}):
            stub = _NamedStubExecutor()
            stub.name = name
            with patch(
                "osimflow.cache._resolve_image_digest",
                side_effect=ImageDigestUnavailableError("docker unavailable"),
            ):
                campaign = Campaign(
                    self._cfg(variables_yml, template_pkg, tmp_path / name, dry_run=True),
                    stub,
                )
                with pytest.raises(RuntimeError, match="requires a digest-pinned container image"):
                    campaign.run()
