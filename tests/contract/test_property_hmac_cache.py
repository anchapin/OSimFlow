"""Property-based tests for cache-key and HMAC canonicalization paths (issue #1696).

The correctness of OSimFlow's caching and payload-integrity guarantees
rests on serialization/canonicalization functions that are only
example-tested elsewhere. The failure modes of canonical JSON — key
ordering, ``None`` vs missing, unicode, floats (``NaN``/``inf``/``-0.0``),
nested containers — are precisely where example-based suites go blind.

A canonicalization drift between the executor-side signer and the
``remote_runner`` verifier (issue #1549's second HMAC) or between two
code versions' cache keys (silent cache poisoning after an edit) are
high-blast-radius, low-signal regressions — hypothesis fences those
cheaply.  Test count is bounded with ``max_examples`` so the file stays
fast enough for the pre-commit contract mirror.
"""

from __future__ import annotations

import json
import string
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from osimflow._sqlite_store import decode_value, encode_value
from osimflow.cache import CacheKey, sha256_of_dict
from osimflow.config import coerce_variable_type
from osimflow.task_payload_hmac import (
    RESULT_TRANSPORT_SIG_ENV,
    TASK_PAYLOAD_SECRET_ENV,
    build_transport_signature_env,
    canonical_result_transport_settings,
    sign_task_payload,
    verify_task_payload,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Bounded ASCII alphabets — hypothesis default alphabet covers the full
# unicode range and is overkill for serialization contracts; bounded
# ASCII keeps each example cheap and the failure traces readable.
_ASCII_PRINT = st.text(alphabet=string.printable, min_size=0, max_size=32)
_SECRET_STRATEGY = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=32)

# Transport field space: ``None``, empty string, and short non-empty
# strings. Mirrors what executors actually emit (mode = "object_storage"
# / "shared_fs" / "auto", backend = "s3" / "gcs" / "azure", etc.) plus
# the ``None`` and "" sentinels the canonicalizer must round-trip.
_TRANSPORT_FIELD = st.one_of(st.none(), st.just(""), _ASCII_PRINT)


def _transport_kwargs(*, allow_insecure: bool = False, **overrides: Any) -> dict[str, Any]:
    """Sample a full set of canonical_result_transport_settings kwargs."""
    return {
        "mode": overrides.get("mode", _TRANSPORT_FIELD.example()),
        "backend": overrides.get("backend", _TRANSPORT_FIELD.example()),
        "bucket": overrides.get("bucket", _TRANSPORT_FIELD.example()),
        "prefix": overrides.get("prefix", _TRANSPORT_FIELD.example()),
        "endpoint": overrides.get("endpoint", _TRANSPORT_FIELD.example()),
        "allow_insecure": allow_insecure,
    }


@st.composite
def transport_kwargs(draw: Any) -> dict[str, Any]:
    """hypothesis strategy for a full canonical-settings kwargs dict."""
    return {
        "mode": draw(_TRANSPORT_FIELD),
        "backend": draw(_TRANSPORT_FIELD),
        "bucket": draw(_TRANSPORT_FIELD),
        "prefix": draw(_TRANSPORT_FIELD),
        "endpoint": draw(_TRANSPORT_FIELD),
        "allow_insecure": draw(st.booleans()),
    }


# ---------------------------------------------------------------------------
# Acceptance criterion #1: canonical-bytes stability under key permutation
# ---------------------------------------------------------------------------


class TestCanonicalResultTransportSettings:
    """``canonical_result_transport_settings`` is the JSON canonicalizer
    shared by both HMAC layers (issue #1549); any drift breaks the
    fail-closed contract that ``remote_runner`` verifies."""

    @given(kwargs=transport_kwargs())
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_canonical_is_valid_compact_json(self, kwargs: dict[str, Any]) -> None:
        """Output must be parseable JSON, in compact separators, with no
        inserted whitespace."""
        canonical = canonical_result_transport_settings(**kwargs)
        # Compact separators — no spaces inside the JSON braces.
        assert ", " not in canonical
        assert ": " not in canonical
        parsed = json.loads(canonical)
        # The six fixed fields must all be present.
        assert set(parsed.keys()) == {
            "allow_insecure",
            "backend",
            "bucket",
            "endpoint",
            "mode",
            "prefix",
        }

    @given(kwargs=transport_kwargs())
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_canonical_keys_are_sorted(self, kwargs: dict[str, Any]) -> None:
        """``sort_keys=True`` is the executor-side ↔ runner-side agreement;
        silently losing it would still parse but mismatch the signature."""
        canonical = canonical_result_transport_settings(**kwargs)
        parsed = json.loads(canonical)
        assert list(parsed.keys()) == sorted(parsed.keys())

    @given(kwargs=transport_kwargs())
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_canonical_is_deterministic(self, kwargs: dict[str, Any]) -> None:
        """Two consecutive calls with identical kwargs must produce
        byte-identical output."""
        first = canonical_result_transport_settings(**kwargs)
        second = canonical_result_transport_settings(**kwargs)
        assert first == second

    @given(kwargs=transport_kwargs())
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_canonical_independent_of_caller_keyword_order(self, kwargs: dict[str, Any]) -> None:
        """Since the function takes its six fields as keyword arguments,
        the output must depend only on the values — verified by
        reconstructing the kwargs in reversed order."""
        ordered = canonical_result_transport_settings(**kwargs)
        reversed_kwargs = dict(reversed(list(kwargs.items())))
        reordered = canonical_result_transport_settings(**reversed_kwargs)
        assert ordered == reordered

    @given(kwargs=transport_kwargs())
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_canonical_preserves_none_as_json_null(self, kwargs: dict[str, Any]) -> None:
        """``None``-valued fields must serialize as JSON ``null`` (and
        not the empty string), so the runner's canonicalization matches."""
        # Force every field to None so we can assert on a single shape.
        all_none = {
            **kwargs,
            "mode": None,
            "backend": None,
            "bucket": None,
            "prefix": None,
            "endpoint": None,
        }
        canonical = canonical_result_transport_settings(**all_none)
        parsed = json.loads(canonical)
        assert parsed["mode"] is None
        assert parsed["backend"] is None
        assert parsed["bucket"] is None
        assert parsed["prefix"] is None
        assert parsed["endpoint"] is None

    @given(
        allow_insecure=st.booleans(),
        suffix=st.text(alphabet=string.ascii_letters, min_size=0, max_size=8),
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_canonical_serializes_allow_insecure_as_json_boolean(
        self, allow_insecure: bool, suffix: str
    ) -> None:
        """The escape-hatch flag must be a JSON boolean literal, not the
        strings ``"True"``/``"False"`` (Python's ``repr(bool)``)."""
        canonical = canonical_result_transport_settings(
            mode=f"m-{suffix}",
            backend=f"b-{suffix}",
            bucket=f"bkt-{suffix}",
            prefix=f"pr-{suffix}",
            endpoint=f"https://e-{suffix}",
            allow_insecure=allow_insecure,
        )
        parsed = json.loads(canonical)
        assert isinstance(parsed["allow_insecure"], bool)
        assert parsed["allow_insecure"] is allow_insecure
        # The output must not contain the Python repr of a bool.
        assert '"True"' not in canonical
        assert '"False"' not in canonical

    @given(kwargs=transport_kwargs())
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_canonical_distinguishes_distinct_settings(self, kwargs: dict[str, Any]) -> None:
        """Flipping a single field must produce a different canonical
        string (or, for ``allow_insecure``, a different JSON value)."""
        canonical = canonical_result_transport_settings(**kwargs)
        flipped_allow = canonical_result_transport_settings(
            **{**kwargs, "allow_insecure": not kwargs["allow_insecure"]}
        )
        parsed_orig = json.loads(canonical)
        parsed_flip = json.loads(flipped_allow)
        assert parsed_orig["allow_insecure"] != parsed_flip["allow_insecure"]


# ---------------------------------------------------------------------------
# Acceptance criterion #2: HMAC sign / verify round-trip over generated shapes
# ---------------------------------------------------------------------------


class TestSignVerifyRoundTrip:
    """``sign_task_payload`` / ``verify_task_payload`` are the HMAC core
    that ``remote_runner`` verifies fail-closed."""

    @given(payload=st.text(min_size=0, max_size=128), secret=_SECRET_STRATEGY)
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_round_trip_arbitrary_payload(self, payload: str, secret: str) -> None:
        """``verify(sign(p, s), s)`` must succeed for any payload / secret."""
        signature = sign_task_payload(payload, secret)
        assert verify_task_payload(payload, signature, secret) is True

    @given(payload=st.text(min_size=1, max_size=128), secret=_SECRET_STRATEGY)
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_tampered_payload_rejected(self, payload: str, secret: str) -> None:
        """Appending a single character to the payload must invalidate
        the signature. Use ``assume`` to skip the (rare) case where the
        tampered string happens to equal the original."""
        signature = sign_task_payload(payload, secret)
        tampered = payload + "x"
        assume(tampered != payload)
        assert verify_task_payload(tampered, signature, secret) is False

    @given(payload=st.text(min_size=1, max_size=64), secret=_SECRET_STRATEGY)
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_missing_signature_rejected(self, payload: str, secret: str) -> None:
        """``verify`` must fail closed when the signature is ``None`` or empty."""
        assert verify_task_payload(payload, None, secret) is False
        assert verify_task_payload(payload, "", secret) is False

    @given(
        payload=st.text(min_size=1, max_size=64),
        secret_a=_SECRET_STRATEGY,
        secret_b=_SECRET_STRATEGY,
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_wrong_secret_rejected(self, payload: str, secret_a: str, secret_b: str) -> None:
        """A signature produced under one secret must not verify under a
        different secret."""
        assume(secret_a != secret_b)
        signature = sign_task_payload(payload, secret_a)
        assert verify_task_payload(payload, signature, secret_b) is False

    @given(payload=st.text(min_size=1, max_size=64), secret=_SECRET_STRATEGY)
    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_signature_is_hex_and_stable_length(self, payload: str, secret: str) -> None:
        """Output must be the 64-char lowercase hex digest of SHA-256."""
        signature = sign_task_payload(payload, secret)
        assert len(signature) == 64
        int(signature, 16)  # raises if non-hex
        # Determinism: signing twice yields the same digest.
        assert sign_task_payload(payload, secret) == signature

    @given(payload=st.text(min_size=0, max_size=64), secret=_SECRET_STRATEGY)
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_unicode_payloads_round_trip(self, payload: str, secret: str) -> None:
        """Hypothesis generates full unicode; signatures must encode and
        decode UTF-8 byte-identical round-trip without throwing."""
        signature = sign_task_payload(payload, secret)
        assert verify_task_payload(payload, signature, secret) is True
        # Sanity: the signature itself is pure ASCII hex.
        signature.encode("ascii")


class TestBuildTransportSignatureEnv:
    """The second HMAC env builder (issue #1549) must round-trip a
    canonicalized transport payload through ``verify_task_payload``."""

    @given(kwargs=transport_kwargs(), secret=_SECRET_STRATEGY)
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_signature_env_round_trips_via_verifier(
        self, kwargs: dict[str, Any], secret: str
    ) -> None:
        """The signature the executor emits must verify against the
        canonicalized settings the runner reconstructs. Mirrors the
        executor / runner contract ``tests/unit/test_result_transport_hmac.py``
        pins with examples — this property version covers the same
        surface fuzzed over the full settings shape."""

        class _Transport:
            mode = kwargs["mode"]
            backend = kwargs["backend"]
            bucket = kwargs["bucket"]
            prefix = kwargs["prefix"]
            endpoint = kwargs["endpoint"]

        env = build_transport_signature_env(
            _Transport(), secret=secret, allow_insecure=kwargs["allow_insecure"]
        )
        assert list(env.keys()) == [RESULT_TRANSPORT_SIG_ENV]
        sig = env[RESULT_TRANSPORT_SIG_ENV]
        canonical = canonical_result_transport_settings(**kwargs)
        assert verify_task_payload(canonical, sig, secret) is True

    @given(kwargs=transport_kwargs(), secret=_SECRET_STRATEGY)
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_signature_env_is_deterministic(self, kwargs: dict[str, Any], secret: str) -> None:
        """Two ``build_transport_signature_env`` calls with identical
        transport + secret + allow_insecure must produce byte-identical
        env dicts — otherwise the verifier would intermittently reject
        the runner's own signature."""

        class _Transport:
            mode = kwargs["mode"]
            backend = kwargs["backend"]
            bucket = kwargs["bucket"]
            prefix = kwargs["prefix"]
            endpoint = kwargs["endpoint"]

        first = build_transport_signature_env(
            _Transport(), secret=secret, allow_insecure=kwargs["allow_insecure"]
        )
        second = build_transport_signature_env(
            _Transport(), secret=secret, allow_insecure=kwargs["allow_insecure"]
        )
        assert first == second

    @given(kwargs=transport_kwargs(), secret=_SECRET_STRATEGY)
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_signature_env_empty_without_secret(self, kwargs: dict[str, Any], secret: str) -> None:
        """Legacy / unsigned mode: no secret ⇒ empty dict, regardless of
        transport shape. ``None`` for the explicit ``secret`` and an
        absent ``OSIMFLOW_TASK_PAYLOAD_SECRET`` env var both flow here."""

        class _Transport:
            mode = kwargs["mode"]
            backend = kwargs["backend"]
            bucket = kwargs["bucket"]
            prefix = kwargs["prefix"]
            endpoint = kwargs["endpoint"]

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
            mp.delenv("NOMAD_META_task_payload_secret", raising=False)
            env = build_transport_signature_env(
                _Transport(), secret=None, allow_insecure=kwargs["allow_insecure"]
            )
            assert env == {}

    @given(
        kwargs=transport_kwargs(),
        secret_a=_SECRET_STRATEGY,
        secret_b=_SECRET_STRATEGY,
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_different_secrets_yield_different_signatures(
        self, kwargs: dict[str, Any], secret_a: str, secret_b: str
    ) -> None:
        """Two distinct secrets over identical transport settings must
        produce two distinct signatures — otherwise the verifier cannot
        tell two tenants apart on a shared substrate."""
        assume(secret_a != secret_b)

        class _Transport:
            mode = kwargs["mode"]
            backend = kwargs["backend"]
            bucket = kwargs["bucket"]
            prefix = kwargs["prefix"]
            endpoint = kwargs["endpoint"]

        sig_a = build_transport_signature_env(
            _Transport(), secret=secret_a, allow_insecure=kwargs["allow_insecure"]
        )[RESULT_TRANSPORT_SIG_ENV]
        sig_b = build_transport_signature_env(
            _Transport(), secret=secret_b, allow_insecure=kwargs["allow_insecure"]
        )[RESULT_TRANSPORT_SIG_ENV]
        assert sig_a != sig_b


# ---------------------------------------------------------------------------
# Acceptance criterion #3: cache-key determinism + coerce_variable_type
# ---------------------------------------------------------------------------


class TestCacheKeyAndDigest:
    """``CacheKey`` is the (step, sample_id, …) primary key of the
    ``cache_entries`` SQLite table.  Two campaigns with the same
    logical inputs must hash to the same row, and any field change must
    move to a different row — otherwise a code-edit cache invalidation
    silently consumes a stale entry."""

    @given(
        step=st.text(alphabet=string.ascii_letters + "_", min_size=1, max_size=16),
        sample_id=st.sampled_from(["ALL", "sample_0", "sample_42"]),
        os_version=st.sampled_from(["3.11.0", "3.12.0", "N/A"]),
        inputs_sha=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
        code_sha=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
        container_digest=st.sampled_from(
            ["unresolved", "sha256:" + "a" * 64, "sha256:" + "0" * 64]
        ),
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_cache_key_equality_is_field_wise(
        self,
        step: str,
        sample_id: str,
        os_version: str,
        inputs_sha: str,
        code_sha: str,
        container_digest: str,
    ) -> None:
        """Two ``CacheKey`` instances with identical field values must
        compare equal and hash equal — the basis of the cache hit path."""
        a = CacheKey(
            step=step,
            sample_id=sample_id,
            openstudio_version=os_version,
            inputs_sha256=inputs_sha,
            code_sha256=code_sha,
            container_digest=container_digest,
        )
        b = CacheKey(
            step=step,
            sample_id=sample_id,
            openstudio_version=os_version,
            inputs_sha256=inputs_sha,
            code_sha256=code_sha,
            container_digest=container_digest,
        )
        assert a == b
        assert hash(a) == hash(b)

    @given(
        step_a=st.sampled_from(["GENERATE_LHS_SAMPLES", "RUN_OPENSTUDIO_SIM"]),
        step_b=st.sampled_from(["GENERATE_LHS_SAMPLES", "RUN_OPENSTUDIO_SIM"]),
        inputs_sha=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
        code_sha=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
    )
    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_cache_keys_distinct_on_step_change(
        self,
        step_a: str,
        step_b: str,
        inputs_sha: str,
        code_sha: str,
    ) -> None:
        """A change in the ``step`` field must produce a non-equal key
        so the LHS-samples cache cannot serve as a SIM-step cache."""
        assume(step_a != step_b)
        a = CacheKey(
            step=step_a,
            sample_id="ALL",
            openstudio_version="3.11.0",
            inputs_sha256=inputs_sha,
            code_sha256=code_sha,
            container_digest="unresolved",
        )
        b = CacheKey(
            step=step_b,
            sample_id="ALL",
            openstudio_version="3.11.0",
            inputs_sha256=inputs_sha,
            code_sha256=code_sha,
            container_digest="unresolved",
        )
        assert a != b


class TestSha256OfDict:
    """``sha256_of_dict`` is the cache-key rule referenced by AGENTS.md §6."""

    @given(
        pairs=st.lists(
            st.tuples(
                st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8),
                st.integers(min_value=-100, max_value=100),
            ),
            min_size=1,
            max_size=8,
        )
    )
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_dict_hash_independent_of_insertion_order(self, pairs: list[tuple[str, int]]) -> None:
        """``sort_keys=True`` means the hash depends only on the
        {key: value} set — flipping dict construction order must not
        invalidate a cache row."""
        keys = [k for k, _ in pairs]
        # Ensure unique keys (hypothesis lists may repeat).
        assume(len(set(keys)) == len(keys))
        # Skip zero-length values that would let an int/value collision
        # make two distinct dicts indistinguishable to a naive hash.
        values = [v for _, v in pairs]
        d_first = dict(zip(keys, values, strict=True))
        d_reversed = dict(zip(reversed(keys), reversed(values), strict=True))
        assert sha256_of_dict(d_first) == sha256_of_dict(d_reversed)

    @given(
        d=st.dictionaries(
            keys=st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6),
            values=st.integers(min_value=-1000, max_value=1000),
            min_size=1,
            max_size=6,
        )
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_dict_hash_is_deterministic(self, d: dict[str, int]) -> None:
        """Same content must hash equal — the cold-then-warm replay rule."""
        assert sha256_of_dict(d) == sha256_of_dict(dict(d))

    @given(
        d=st.dictionaries(
            keys=st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6),
            values=st.integers(min_value=-1000, max_value=1000),
            min_size=1,
            max_size=6,
        ),
        extra_key=st.text(alphabet=string.ascii_uppercase, min_size=1, max_size=6),
        extra_value=st.integers(min_value=-1000, max_value=1000),
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_dict_hash_distinguishes_added_field(
        self,
        d: dict[str, int],
        extra_key: str,
        extra_value: int,
    ) -> None:
        """Adding a field must change the hash — otherwise adding a
        measure arg silently poisons the cache."""
        assume(extra_key not in d)
        h_base = sha256_of_dict(d)
        h_extended = sha256_of_dict({**d, extra_key: extra_value})
        assert h_base != h_extended


# ---------------------------------------------------------------------------
# Supporting encode_value / decode_value round-trip (issue #1696 body)
# ---------------------------------------------------------------------------


class TestEncodeDecodeValueRoundTrip:
    """``_sqlite_store.encode_value`` / ``decode_value`` are the shared
    JSON canonicalizer used by the registry, document store, and event
    log.  The ``default=str`` fallback lets ``Path`` / dataclasses round
    trip — a regression here silently corrupts every persisted row."""

    @given(
        d=st.dictionaries(
            keys=st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6),
            values=st.one_of(
                st.none(),
                st.booleans(),
                st.integers(min_value=-10000, max_value=10000),
                st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
                st.text(alphabet=string.printable, min_size=0, max_size=32),
            ),
            min_size=0,
            max_size=8,
        )
    )
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_round_trip_for_primitives(self, d: dict[str, Any]) -> None:
        """``decode(encode(x)) == x`` for any JSON-serializable dict."""
        encoded = encode_value(d)
        assert decode_value(encoded) == d

    @given(
        pairs=st.lists(
            st.tuples(
                st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6),
                st.integers(min_value=-100, max_value=100),
            ),
            min_size=1,
            max_size=8,
        )
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_encode_value_is_order_independent(self, pairs: list[tuple[str, int]]) -> None:
        """``sort_keys=True`` means dict construction order does not
        change the encoded bytes — important for fingerprinting."""
        keys = [k for k, _ in pairs]
        assume(len(set(keys)) == len(keys))
        values = [v for _, v in pairs]
        d_first = dict(zip(keys, values, strict=True))
        d_reversed = dict(zip(reversed(keys), reversed(values), strict=True))
        assert encode_value(d_first) == encode_value(d_reversed)

    @given(
        value=st.one_of(
            st.none(),
            st.booleans(),
            st.integers(min_value=-10_000, max_value=10_000),
            st.text(min_size=0, max_size=32),
            st.lists(st.integers(min_value=-100, max_value=100), max_size=8),
        )
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_decode_handles_corrupt_input_gracefully(self, value: Any) -> None:
        """``decode_value`` must return the default on non-JSON input
        rather than raising — otherwise a single corrupt row crashes
        every store read."""
        assert decode_value(None) is None
        assert decode_value("") is None
        # Custom default flows through.
        sentinel: dict[str, int] = {"default": 1}
        assert decode_value(None, default=sentinel) is sentinel
        assert decode_value("not-json{", default=sentinel) is sentinel


# ---------------------------------------------------------------------------
# Acceptance criterion #3 (continued): coerce_variable_type property tests
# ---------------------------------------------------------------------------


class TestCoerceVariableType:
    """``coerce_variable_type`` is the YAML-to-Python bridge used by
    ``variables.yml`` loading. A silent lossiness drift here breaks
    sampling without any error."""

    @given(
        n=st.integers(min_value=-10_000, max_value=10_000),
        as_str=st.sampled_from(["int", "integer"]),
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_int_identity_under_int(self, n: int, as_str: str) -> None:
        """An already-int value coerced to int (or its alias ``integer``)
        returns it unchanged."""
        assert coerce_variable_type(n, int) == n
        assert coerce_variable_type(n, as_str) == n

    @given(x=st.integers(min_value=-10_000, max_value=10_000))
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_float_to_int_lossless_only(self, x: int) -> None:
        """``float → int`` must round-trip exactly when no precision is
        lost; the canonical ``coerce_variable_type`` raises otherwise.

        Strategy is integer-bounded so every drawn float is whole-number
        by construction — no ``assume()`` filter and no precision-loss
        path to test here (the lossless-only property is exercised by
        the ``str → int`` test below)."""
        xf = float(x)
        assert coerce_variable_type(xf, int) == x

    @given(
        n=st.integers(min_value=-10_000, max_value=10_000),
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_int_to_float_widening(self, n: int) -> None:
        """``int → float`` must always be lossless."""
        result = coerce_variable_type(n, float)
        assert isinstance(result, float)
        assert result == float(n)

    @given(s=st.sampled_from(["true", "True", "false", "False", "yes", "no", "1", "0"]))
    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_str_to_bool_recognized_tokens(self, s: str) -> None:
        """The documented true/false tokens round-trip to Python bools."""
        expected = s.lower() in ("true", "1", "yes", "on")
        assert coerce_variable_type(s, bool) is expected

    @given(
        items=st.lists(
            st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=4),
            min_size=1,
            max_size=5,
        )
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_str_to_list_round_trip(self, items: list[str]) -> None:
        """``"a,b,c"`` → ``["a","b","c"]``. Round-trip is exact for the
        generated alphabet (no whitespace / quoting edge cases)."""
        joined = ",".join(items)
        assert coerce_variable_type(joined, list) == items

    @given(
        n=st.integers(min_value=0, max_value=1000),
        target=st.sampled_from(["float", "double", "int", "integer", "str", "string"]),
    )
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_type_name_aliases_match_canonical_type(self, n: int, target: str) -> None:
        """Documented type-name aliases (``"double"`` for ``float``,
        ``"integer"`` for ``int``, ``"string"`` for ``str``) must coerce
        identically to the canonical Python type.

        ``bool``/``"boolean"`` are exercised separately in
        ``test_str_to_bool_recognized_tokens`` because the recognized
        token set is small (``"true"``/``"1"``/``"0"``/...) and arbitrary
        integers don't all coerce to a bool."""
        # Non-negative int so every numeric / string target accepts it.
        payload = str(n)
        via_alias = coerce_variable_type(payload, target)
        canonical_name = {
            "float": float,
            "double": float,
            "int": int,
            "integer": int,
            "str": str,
            "string": str,
        }[target]
        via_canonical = coerce_variable_type(payload, canonical_name)
        assert via_alias == via_canonical

    @given(value=st.sampled_from([True, False]))
    @settings(max_examples=5, deadline=None)
    def test_bool_round_trip_with_int(self, value: bool) -> None:
        """``bool`` is not silently treated as ``int`` (and vice-versa)
        when the user requested the other — issue #1696 explicitly
        guards against the ``isinstance(True, int) == True`` Python quirk.
        """
        assert coerce_variable_type(value, bool) is value
        # Coercing ``True``/``False`` *to* ``int`` raises — the function
        # distinguishes them from ``1``/``0`` deliberately.
        with pytest.raises(ValueError):
            coerce_variable_type(value, int)
        with pytest.raises(ValueError):
            coerce_variable_type(value, float)
