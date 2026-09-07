"""Unit tests for the ``ResultTransportConfig`` value object (issue #1541).

The frozen dataclass collapses the five historic result-transport
parameters (``result_transport_mode`` + the four ``result_storage_*``
fields) into a single value threaded through ``submit()`` and every
remote ``Handle``. These tests pin:

* construction defaults matching the historic per-field defaults;
* the single coercion point (``__post_init__`` normalizes alias
  strings like ``"object-storage"`` / ``"sharedfs"``);
* immutability (frozen; mutation raises ``FrozenInstanceError``);
* ``from_kwargs`` / ``from_campaign_config`` factories;
* the ``resolve_transport_argument`` legacy-kwargs shim, including
  the both-forms-supplied conflict error;
* ``SubmitRequest``'s read-through back-compat properties.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from osimflow.config import CampaignConfig
from osimflow.executors.base import SubmitRequest
from osimflow.executors.transport import (
    ResultTransportConfig,
    resolve_transport_argument,
)


class TestConstruction:
    def test_defaults_match_historic_per_field_defaults(self) -> None:
        config = ResultTransportConfig()
        assert config.mode == "auto"
        assert config.backend is None
        assert config.bucket is None
        assert config.prefix is None
        assert config.endpoint is None
        assert config.presigned_url_expiration_s is None

    def test_explicit_fields_round_trip(self) -> None:
        config = ResultTransportConfig(
            mode="object_storage",
            backend="s3",
            bucket="bucket-a",
            prefix="campaigns/c1",
            endpoint="https://s3.example.test",
            presigned_url_expiration_s=7200,
        )
        assert config.mode == "object_storage"
        assert config.backend == "s3"
        assert config.bucket == "bucket-a"
        assert config.prefix == "campaigns/c1"
        assert config.endpoint == "https://s3.example.test"
        assert config.presigned_url_expiration_s == 7200

    def test_equality_is_field_wise(self) -> None:
        a = ResultTransportConfig(mode="shared_fs")
        b = ResultTransportConfig(mode="shared_fs")
        c = ResultTransportConfig(mode="object_storage", backend="s3")
        assert a == b
        assert hash(a) == hash(b)
        assert a != c


class TestCoercion:
    def test_post_init_coerces_alias_strings(self) -> None:
        assert ResultTransportConfig(mode="object-storage").mode == "object_storage"
        assert ResultTransportConfig(mode="objectstorage").mode == "object_storage"
        assert ResultTransportConfig(mode="shared-fs").mode == "shared_fs"
        assert ResultTransportConfig(mode="sharedfs").mode == "shared_fs"

    def test_post_init_coerces_none_and_unknown_to_auto(self) -> None:
        # Mirrors coerce_transport_mode: None / unrecognized strings fall
        # back to "auto" rather than raising (validation against the
        # capability matrix happens separately at submit time).
        assert ResultTransportConfig(mode=None).mode == "auto"  # type: ignore[arg-type]
        assert ResultTransportConfig(mode="nonsense").mode == "auto"


class TestImmutability:
    def test_frozen_dataclass_rejects_mutation(self) -> None:
        config = ResultTransportConfig(mode="shared_fs")
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.mode = "object_storage"  # type: ignore[misc]

    def test_frozen_dataclass_rejects_new_fields(self) -> None:
        config = ResultTransportConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.backend = "s3"  # type: ignore[misc]


class TestFromKwargs:
    def test_maps_the_five_legacy_fields(self) -> None:
        config = ResultTransportConfig.from_kwargs(
            result_transport_mode="object-storage",
            result_storage_backend="s3",
            result_storage_bucket="bucket-a",
            result_storage_prefix="campaigns/c1",
            result_storage_endpoint="https://s3.example.test",
        )
        assert config == ResultTransportConfig(
            mode="object_storage",
            backend="s3",
            bucket="bucket-a",
            prefix="campaigns/c1",
            endpoint="https://s3.example.test",
        )

    def test_unset_legacy_fields_default_to_auto_and_none(self) -> None:
        assert ResultTransportConfig.from_kwargs() == ResultTransportConfig()


class TestFromCampaignConfig:
    def _cfg(self, tmp_path: Path) -> CampaignConfig:
        return CampaignConfig(
            input_variables=tmp_path / "variables.yml",
            template_sim_package=tmp_path / "template",
            n_samples=1,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
            result_storage_backend="s3",
            result_storage_bucket="campaign-bucket",
            result_storage_endpoint="https://s3.example.test",
            s3_artifact_presigned_url_expiration=1800,
        )

    def test_shared_fs_mode_when_not_object_storage(self, tmp_path: Path) -> None:
        config = ResultTransportConfig.from_campaign_config(
            self._cfg(tmp_path), object_storage=False
        )
        assert config == ResultTransportConfig(mode="shared_fs")

    def test_object_storage_mode_maps_campaign_fields(self, tmp_path: Path) -> None:
        cfg = self._cfg(tmp_path)
        config = ResultTransportConfig.from_campaign_config(cfg, object_storage=True)
        assert config.mode == "object_storage"
        assert config.backend == "s3"
        assert config.bucket == "campaign-bucket"
        assert config.prefix == str(cfg.outdir.name)
        assert config.endpoint == "https://s3.example.test"
        assert config.presigned_url_expiration_s == 1800


class TestResolveTransportArgument:
    def test_none_everywhere_returns_none(self) -> None:
        assert resolve_transport_argument(None) is None

    def test_config_passes_through_untouched(self) -> None:
        config = ResultTransportConfig(mode="shared_fs")
        assert resolve_transport_argument(config) is config

    def test_legacy_kwargs_build_config(self) -> None:
        config = resolve_transport_argument(
            None,
            result_transport_mode="object_storage",
            result_storage_backend="s3",
            result_storage_bucket="bucket-a",
            result_storage_prefix="out",
            result_storage_endpoint=None,
        )
        assert config == ResultTransportConfig(
            mode="object_storage",
            backend="s3",
            bucket="bucket-a",
            prefix="out",
        )

    def test_both_forms_raises(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            resolve_transport_argument(
                ResultTransportConfig(mode="shared_fs"),
                result_transport_mode="object_storage",
            )


class TestSubmitRequestBackCompat:
    def test_read_through_properties_expose_config_fields(self) -> None:
        request = SubmitRequest(
            fn=lambda: None,
            transport=ResultTransportConfig(
                mode="object_storage",
                backend="s3",
                bucket="bucket-a",
                prefix="out",
                endpoint="https://s3.example.test",
            ),
        )
        assert request.result_transport_mode == "object_storage"
        assert request.result_storage_backend == "s3"
        assert request.result_storage_bucket == "bucket-a"
        assert request.result_storage_prefix == "out"
        assert request.result_storage_endpoint == "https://s3.example.test"

    def test_read_through_properties_return_none_without_config(self) -> None:
        request = SubmitRequest(fn=lambda: None)
        assert request.transport is None
        assert request.result_transport_mode is None
        assert request.result_storage_backend is None
        assert request.result_storage_bucket is None
        assert request.result_storage_prefix is None
        assert request.result_storage_endpoint is None
