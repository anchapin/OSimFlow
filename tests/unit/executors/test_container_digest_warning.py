"""Regression tests for the mutable-tag supply-chain warning (issue #1320).

Cloud executors (`aws_batch`, `azure_batch`, `google_batch`) pull
`nrel/openstudio:<version>` by mutable tag when `--container-digest` is
not set. The CLI must warn so operators pin a digest for production;
the docs make digest pinning the documented production default.
"""

from __future__ import annotations

import pytest

from osimflow.__main__ import _CLOUD_EXECUTORS, _build_parser, _warn_if_mutable_tag

_WARN_MATCH = "MUTABLE tag"


@pytest.mark.parametrize("executor_name", sorted(_CLOUD_EXECUTORS))
def test_cloud_executor_without_digest_warns(executor_name: str) -> None:
    with pytest.warns(UserWarning, match=_WARN_MATCH):
        _warn_if_mutable_tag(executor_name, container_digest=None)


@pytest.mark.parametrize("executor_name", sorted(_CLOUD_EXECUTORS))
def test_cloud_executor_with_digest_is_silent(executor_name: str) -> None:
    import warnings as warnings_mod

    with warnings_mod.catch_warnings():
        warnings_mod.simplefilter("error")
        _warn_if_mutable_tag(executor_name, container_digest="sha256:" + "a" * 64)


def test_local_executor_without_digest_is_silent() -> None:
    import warnings as warnings_mod

    with warnings_mod.catch_warnings():
        warnings_mod.simplefilter("error")
        _warn_if_mutable_tag("local", container_digest=None)


def test_cloud_executor_set_is_exactly_the_managed_batch_services() -> None:
    assert _CLOUD_EXECUTORS == frozenset({"aws_batch", "azure_batch", "google_batch"})


def test_parser_accepts_container_digest_with_cloud_executor() -> None:
    args = vars(
        _build_parser().parse_args(
            [
                "run",
                "--executor",
                "aws_batch",
                "--input_variables",
                "v.yml",
                "--template_sim_package",
                "pkg",
                "--n_samples",
                "1",
                "--outdir",
                "out",
                "--container-digest",
                "sha256:" + "a" * 64,
            ]
        )
    )
    assert args["container_digest"] == "sha256:" + "a" * 64
