from __future__ import annotations

from osimflow import NomadExecutor
from osimflow.__main__ import _build_executor, _build_parser


def _base_run_args() -> list[str]:
    return [
        "run",
        "--executor",
        "nomad",
        "--input_variables",
        "variables.yml",
        "--template_sim_package",
        "example_package",
        "--n_samples",
        "1",
        "--outdir",
        "results",
    ]


def test_nomad_remote_results_only_defaults_true() -> None:
    parser = _build_parser()
    args = parser.parse_args(_base_run_args())
    assert args.nomad_remote_results_only is True


def test_nomad_remote_results_only_can_be_disabled_for_compatibility() -> None:
    parser = _build_parser()
    args = parser.parse_args([*_base_run_args(), "--no-nomad-remote-results-only"])
    assert args.nomad_remote_results_only is False


def test_nomad_scale_flags_have_backward_compatible_defaults() -> None:
    parser = _build_parser()
    args = parser.parse_args(_base_run_args())
    assert args.nomad_dispatch_policy == "keep_manual"
    assert args.nomad_allocation_resolution_timeout_s == 30.0
    assert args.nomad_poll_interval_s == 5.0
    assert args.nomad_max_poll_interval_s == 60.0
    assert args.nomad_fanout_submit_rate_per_sec is None
    assert args.nomad_fanout_submit_chunk_size == 0
    assert args.shard_count is None
    assert args.shard_index is None
    assert args.shard_start is None
    assert args.shard_end is None


def test_nomad_scale_flags_parse_and_wire_to_executor() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            *_base_run_args(),
            "--nomad-dispatch-policy",
            "force_dispatch",
            "--nomad-allocation-resolution-timeout-s",
            "42.5",
            "--nomad-poll-interval-s",
            "2.5",
            "--nomad-max-poll-interval-s",
            "15.0",
            "--nomad-fanout-submit-rate-per-sec",
            "8.0",
            "--nomad-fanout-submit-chunk-size",
            "12",
            "--shard-count",
            "4",
            "--shard-index",
            "1",
        ]
    )
    assert args.nomad_fanout_submit_rate_per_sec == 8.0
    assert args.nomad_fanout_submit_chunk_size == 12
    assert args.shard_count == 4
    assert args.shard_index == 1
    executor = _build_executor(args)
    assert isinstance(executor, NomadExecutor)
    assert executor.dispatch_policy == "force_dispatch"
    assert executor.use_dispatch is True
    assert executor.allocation_resolution_timeout_s == 42.5
    assert executor.poll_interval_s == 2.5
    assert executor.max_poll_interval_s == 15.0
    assert executor.fanout_submit_rate_per_sec == 8.0
    assert executor.fanout_submit_chunk_size(25) == 12
    assert executor.estimated_run_size == 1


def test_nomad_range_sharding_flags_parse() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            *_base_run_args(),
            "--shard-start",
            "100",
            "--shard-end",
            "200",
        ]
    )
    assert args.shard_start == 100
    assert args.shard_end == 200


def test_nomad_tls_defaults_off() -> None:
    """--nomad-tls keeps its backwards-compatible default of False."""
    parser = _build_parser()
    args = parser.parse_args(_base_run_args())
    assert args.nomad_tls is False


def test_nomad_cleartext_token_warns_for_non_local_address() -> None:
    """Constructing a NomadExecutor with TLS off + non-local address warns loudly (#1112)."""
    import pytest

    with pytest.warns(UserWarning, match="SEC-009"):
        NomadExecutor(address="https://nomad.example.com:4646", tls=False)


def test_nomad_no_warning_for_local_address() -> None:
    """Loopback addresses are exempt from the cleartext-token warning."""
    import warnings as warnings_mod

    with warnings_mod.catch_warnings():
        warnings_mod.simplefilter("error", UserWarning)
        ex = NomadExecutor(address="http://127.0.0.1:4646", tls=False)
    assert ex.tls is False


def test_nomad_no_warning_when_tls_enabled() -> None:
    """TLS-enabled non-local addresses do not warn."""
    import warnings as warnings_mod

    with warnings_mod.catch_warnings():
        warnings_mod.simplefilter("error", UserWarning)
        ex = NomadExecutor(
            address="https://nomad.example.com:4646",
            tls=True,
            cert="/tmp/cert.pem",
            key="/tmp/key.pem",
        )
    assert ex.tls is True


def test_is_local_address_variants() -> None:
    """_is_local_address recognizes loopback hostnames and IPv6 variants."""
    is_local = NomadExecutor._is_local_address
    assert is_local("http://127.0.0.1:4646") is True
    assert is_local("http://localhost:4646") is True
    assert is_local("http://LOCALHOST:4646") is True
    assert is_local("http://[::1]:4646") is True
    assert is_local("127.0.0.1:4646") is True
    assert is_local("https://nomad.example.com:4646") is False
    assert is_local("http://10.0.0.5:4646") is False
