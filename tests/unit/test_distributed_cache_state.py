"""Unit tests for the distributed campaign-state backend (issue #993, T8.2).

Covers the migration of shared campaign state from a contended local
SQLite file to the Redis-backed shared entry store in
``osimflow/distributed_cache.py``:

- ``campaign_state_namespace``: stable per-outdir, isolated across outdirs
- ``build_cache`` wiring inside ``Campaign`` (default ``SQLiteCache`` vs
  ``DistributedCache`` when ``redis_url`` is set)
- Shared entry store: a store on one cache is visible to another (Redis
  fallback path, local backfill, CacheStats accounting)
- Shared invalidation: HDEL from the shared store + existing pub/sub
- Per-process local SQLite files (no two processes share one file)
- Graceful degradation when Redis is unreachable
- The T8.1 lock-reproducer analog (fluxion#1790): multiple *concurrent
  processes* coordinating shared state through the distributed backend
  with no SQLite lock contention and no shared SQLite file — using a
  fake Redis layer, so no live Redis is required in CI.
"""

from __future__ import annotations

import fnmatch
import multiprocessing
import os
import threading
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.cache import CacheKey, SQLiteCache
from osimflow.config import load_config
from osimflow.distributed_cache import (
    DistributedCache,
    campaign_state_namespace,
)
from osimflow.executors import LocalExecutor

# ---------------------------------------------------------------------
# Fake sync Redis client (no live Redis, no `redis` extra required)
# ---------------------------------------------------------------------


class FakeSyncRedis:
    """Minimal dict-backed stand-in for the sync ``redis.Redis`` client.

    Implements exactly the commands the shared entry store uses:
    ``hset``, ``hget``, ``hdel``, ``hscan_iter``, ``hlen``, ``close``.
    ``storage`` may be a plain dict (single process) or a
    ``multiprocessing.Manager().dict()`` proxy (shared across processes).
    """

    def __init__(self, storage: MutableMapping[str, str] | None = None) -> None:
        self._storage: MutableMapping[str, str] = storage if storage is not None else {}
        self._lock = threading.Lock()

    @staticmethod
    def _k(name: str, field: str) -> str:
        return f"{name}\x00{field}"

    def hset(self, name: str, key: str, value: str) -> None:
        with self._lock:
            self._storage[self._k(name, key)] = value

    def hget(self, name: str, key: str) -> str | None:
        with self._lock:
            return self._storage.get(self._k(name, key))

    def hdel(self, name: str, *keys: str) -> int:
        with self._lock:
            n = 0
            for key in keys:
                if self._storage.pop(self._k(name, key), None) is not None:
                    n += 1
            return n

    def hscan_iter(self, name: str, match: str | None = None) -> Iterator[tuple[str, str]]:
        prefix = f"{name}\x00"
        with self._lock:
            items = [
                (k[len(prefix) :], v)
                for k, v in list(self._storage.items())
                if k.startswith(prefix)
            ]
        for field, value in items:
            if match is None or fnmatch.fnmatchcase(field, match):
                yield field, value

    def hlen(self, name: str) -> int:
        return sum(1 for _ in self.hscan_iter(name))

    def close(self) -> None:
        pass


def _fake_redis_module(client: FakeSyncRedis) -> MagicMock:
    """Build a fake ``redis`` module whose ``from_url`` returns ``client``."""
    module = MagicMock()
    module.from_url.return_value = client
    return module


def _key(step: str = "RUN_OPENSTUDIO_SIM", sample_id: str = "s0001", **overrides: Any) -> CacheKey:
    defaults: dict[str, Any] = {
        "step": step,
        "sample_id": sample_id,
        "openstudio_version": "3.11.0",
        "inputs_sha256": "inputs",
        "code_sha256": "code",
        "container_digest": "py",
        "generation": 0,
    }
    defaults.update(overrides)
    return CacheKey(**defaults)


# ---------------------------------------------------------------------
# Namespace helper
# ---------------------------------------------------------------------


class TestCampaignStateNamespace:
    def test_stable_for_same_outdir(self, tmp_path: Path) -> None:
        a = campaign_state_namespace(tmp_path / "results")
        b = campaign_state_namespace(tmp_path / "results")
        assert a == b

    def test_different_for_different_outdirs(self, tmp_path: Path) -> None:
        a = campaign_state_namespace(tmp_path / "run_a")
        b = campaign_state_namespace(tmp_path / "run_b")
        assert a != b

    def test_shape(self, tmp_path: Path) -> None:
        ns = campaign_state_namespace(tmp_path / "results")
        assert ns.startswith("outdir-")
        # 16 hex chars of SHA-256 — short, deterministic, filesystem-free.
        assert len(ns) == len("outdir-") + 16


# ---------------------------------------------------------------------
# Shared entry store (single-process, two caches = two "nodes")
# ---------------------------------------------------------------------


class TestSharedEntryStore:
    @pytest.fixture
    def shared_fake(self) -> FakeSyncRedis:
        return FakeSyncRedis()

    def _make_cache(self, db_path: Path, fake: FakeSyncRedis) -> DistributedCache:
        with patch(
            "osimflow.distributed_cache._get_redis_sync",
            return_value=_fake_redis_module(fake),
        ):
            cache = DistributedCache(
                db_path=db_path,
                redis_url="redis://localhost:6379/0",
                campaign_id="unit-shared",
            )
        # Pre-seed the (lazily created) sync client so the shared-store
        # data plane never imports the real redis package, which is an
        # optional extra and not installed in CI.
        cache._sync_client = fake  # type: ignore[assignment]
        return cache

    def test_store_on_one_cache_visible_to_another(
        self, tmp_path: Path, shared_fake: FakeSyncRedis
    ) -> None:
        node_a = self._make_cache(tmp_path / "a" / "cache.sqlite", shared_fake)
        node_b = self._make_cache(tmp_path / "b" / "cache.sqlite", shared_fake)
        out = tmp_path / "out"
        out.mkdir()
        key = _key()
        node_a.store(key, out, exit_code=0)
        # Node B's local SQLite never saw this key — the hit comes from
        # the Redis shared store.
        assert node_b.lookup(key) == out

    def test_shared_hit_backfills_local(self, tmp_path: Path, shared_fake: FakeSyncRedis) -> None:
        node_a = self._make_cache(tmp_path / "a" / "cache.sqlite", shared_fake)
        node_b = self._make_cache(tmp_path / "b" / "cache.sqlite", shared_fake)
        out = tmp_path / "out"
        out.mkdir()
        key = _key()
        node_a.store(key, out, exit_code=0)
        assert node_b.lookup(key) == out
        # The shared hit was backfilled into B's local file, so a second
        # lookup is served locally even if Redis goes away.
        node_b._sync_client = None  # simulate: drop the (fake) client
        with patch(
            "osimflow.distributed_cache._get_redis_sync",
            side_effect=ConnectionError("redis gone"),
        ):
            assert node_b._local.lookup(key) == out

    def test_shared_hit_accounted_in_stats(
        self, tmp_path: Path, shared_fake: FakeSyncRedis
    ) -> None:
        node_a = self._make_cache(tmp_path / "a" / "cache.sqlite", shared_fake)
        node_b = self._make_cache(tmp_path / "b" / "cache.sqlite", shared_fake)
        out = tmp_path / "out"
        out.mkdir()
        node_a.store(_key(), out, exit_code=0)
        assert node_b.lookup(_key()) == out
        stats = node_b.get_stats()
        assert stats.hits == 1
        assert stats.misses == 0

    def test_failed_exit_code_is_not_a_shared_hit(
        self, tmp_path: Path, shared_fake: FakeSyncRedis
    ) -> None:
        node_a = self._make_cache(tmp_path / "a" / "cache.sqlite", shared_fake)
        node_b = self._make_cache(tmp_path / "b" / "cache.sqlite", shared_fake)
        out = tmp_path / "out"
        out.mkdir()
        key = _key()
        node_a.store(key, out, exit_code=1)
        assert node_b.lookup(key) is None

    def test_missing_output_is_not_a_shared_hit(
        self, tmp_path: Path, shared_fake: FakeSyncRedis
    ) -> None:
        node_a = self._make_cache(tmp_path / "a" / "cache.sqlite", shared_fake)
        node_b = self._make_cache(tmp_path / "b" / "cache.sqlite", shared_fake)
        stale = tmp_path / "vanished"
        stale.mkdir()
        key = _key()
        node_a.store(key, stale, exit_code=0)
        stale.rmdir()
        assert node_b.lookup(key) is None

    def test_corrupt_shared_entry_is_miss(self, tmp_path: Path, shared_fake: FakeSyncRedis) -> None:
        node_b = self._make_cache(tmp_path / "b" / "cache.sqlite", shared_fake)
        # Write garbage directly into the shared store.
        shared_fake.hset("osimflow:cache:entries:unit-shared", _key().step, "not-json{")
        assert node_b.lookup(_key()) is None

    def test_degrades_to_local_when_redis_down(self, tmp_path: Path) -> None:
        failing = MagicMock()
        failing.from_url.side_effect = ConnectionError("redis down")
        with patch("osimflow.distributed_cache._get_redis_sync", return_value=failing):
            cache = DistributedCache(
                db_path=tmp_path / "cache.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="unit-degrade",
            )
            out = tmp_path / "out"
            out.mkdir()
            key = _key()
            cache.store(key, out, exit_code=0)  # must not raise
            assert cache.lookup(key) == out  # local round-trip still works


# ---------------------------------------------------------------------
# Shared invalidation
# ---------------------------------------------------------------------


class TestSharedInvalidation:
    @pytest.fixture(autouse=True)
    def _isolate_pubsub(self) -> Any:
        """Keep the async subscriber thread out of these unit tests."""
        with (
            patch.object(DistributedCache, "_start_subscriber"),
            patch.object(DistributedCache, "_publish"),
        ):
            yield

    @pytest.fixture
    def shared_fake(self) -> FakeSyncRedis:
        return FakeSyncRedis()

    def _make_cache(self, db_path: Path, fake: FakeSyncRedis) -> DistributedCache:
        with patch(
            "osimflow.distributed_cache._get_redis_sync",
            return_value=_fake_redis_module(fake),
        ):
            cache = DistributedCache(
                db_path=db_path,
                redis_url="redis://localhost:6379/0",
                campaign_id="unit-invalidate",
            )
        cache._sync_client = fake  # type: ignore[assignment]
        return cache

    def test_invalidate_step_removes_shared_entries(
        self, tmp_path: Path, shared_fake: FakeSyncRedis
    ) -> None:
        node_a = self._make_cache(tmp_path / "a" / "cache.sqlite", shared_fake)
        out = tmp_path / "out"
        out.mkdir()
        node_a.store(_key(step="STEP_A", sample_id="s1"), out, exit_code=0)
        node_a.store(_key(step="STEP_A", sample_id="s2"), out, exit_code=0)
        node_a.store(_key(step="STEP_B", sample_id="s1"), out, exit_code=0)

        n = node_a.invalidate_step("STEP_A")
        assert n == 2
        # The shared store no longer holds STEP_A but keeps STEP_B.
        assert shared_fake.hlen("osimflow:cache:entries:unit-invalidate") == 1

        # A freshly-joining process (empty local file) must see the
        # invalidation: STEP_A misses, STEP_B still hits via Redis.
        node_c = self._make_cache(tmp_path / "c" / "cache.sqlite", shared_fake)
        assert node_c.lookup(_key(step="STEP_A", sample_id="s1")) is None
        assert node_c.lookup(_key(step="STEP_B", sample_id="s1")) == out

    def test_invalidate_sample_removes_shared_entries(
        self, tmp_path: Path, shared_fake: FakeSyncRedis
    ) -> None:
        node_a = self._make_cache(tmp_path / "a" / "cache.sqlite", shared_fake)
        out = tmp_path / "out"
        out.mkdir()
        node_a.store(_key(step="STEP_A", sample_id="s1"), out, exit_code=0)
        node_a.store(_key(step="STEP_A", sample_id="s2"), out, exit_code=0)

        n = node_a.invalidate_sample("STEP_A", "s1")
        assert n == 1
        node_c = self._make_cache(tmp_path / "c" / "cache.sqlite", shared_fake)
        assert node_c.lookup(_key(step="STEP_A", sample_id="s1")) is None
        assert node_c.lookup(_key(step="STEP_A", sample_id="s2")) == out


# ---------------------------------------------------------------------
# Per-process local SQLite files
# ---------------------------------------------------------------------


class TestPerProcessLocalDb:
    def test_local_db_is_pid_private(self, tmp_path: Path) -> None:
        with patch(
            "osimflow.distributed_cache._get_redis_sync",
            return_value=_fake_redis_module(FakeSyncRedis()),
        ):
            cache = DistributedCache(
                db_path=tmp_path / "cache.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="unit-pid",
            )
        expected = tmp_path / f"cache.p{os.getpid()}.sqlite"
        assert cache._local.db_path == expected
        # The .sqlite suffix survives so artifact-manifest categorisation
        # (which keys on suffix) still classifies the file as cache.
        assert cache._local.db_path.suffix == ".sqlite"
        assert cache._local.db_path.exists()

    def test_requested_db_path_preserved(self, tmp_path: Path) -> None:
        with patch(
            "osimflow.distributed_cache._get_redis_sync",
            return_value=_fake_redis_module(FakeSyncRedis()),
        ):
            cache = DistributedCache(
                db_path=tmp_path / "cache.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="unit-pid",
            )
        assert cache.requested_db_path == tmp_path / "cache.sqlite"

    def test_no_shared_sqlite_file_created(self, tmp_path: Path) -> None:
        """Two caches on one requested path never open the same file."""
        with patch(
            "osimflow.distributed_cache._get_redis_sync",
            return_value=_fake_redis_module(FakeSyncRedis()),
        ):
            for _ in range(2):
                DistributedCache(
                    db_path=tmp_path / "cache.sqlite",
                    redis_url="redis://localhost:6379/0",
                    campaign_id="unit-pid",
                )
        assert not (tmp_path / "cache.sqlite").exists()
        assert list(tmp_path.glob("cache.p*.sqlite"))


# ---------------------------------------------------------------------
# SQLiteCache.note_external_hit (stats accounting for shared hits)
# ---------------------------------------------------------------------


class TestNoteExternalHit:
    def test_reclassifies_miss_as_hit(self, tmp_path: Path) -> None:
        cache = SQLiteCache(tmp_path / "cache.sqlite")
        key = _key()
        assert cache.lookup(key) is None  # one miss
        cache.note_external_hit()
        stats = cache.get_stats()
        assert stats.hits == 1
        assert stats.misses == 0

    def test_misses_never_negative(self, tmp_path: Path) -> None:
        cache = SQLiteCache(tmp_path / "cache.sqlite")
        cache.note_external_hit()  # no prior miss — clamp at zero
        assert cache.get_stats().misses == 0
        assert cache.get_stats().hits == 1


# ---------------------------------------------------------------------
# Campaign wiring (issue #993 acceptance criteria a + b)
# ---------------------------------------------------------------------


def _campaign_cfg(
    variables_yml: Path, template_pkg: Path, outdir: Path, **overrides: Any
) -> CampaignConfig:
    defaults: dict[str, Any] = {
        "input_variables": variables_yml,
        "template_sim_package": template_pkg,
        "n_samples": 3,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "dry_run": True,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


class TestCampaignCacheBackendSelection:
    def test_default_is_plain_sqlite(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _campaign_cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        # Acceptance (b): single-node default is unchanged — a plain
        # SQLiteCache at exactly cfg.cache_db (no pid suffix).
        assert isinstance(campaign.cache, SQLiteCache)
        assert not isinstance(campaign.cache, DistributedCache)
        assert campaign.cache.db_path == cfg.cache_db
        campaign.cache.close()

    def test_redis_url_selects_distributed_cache(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _campaign_cfg(
            variables_yml, template_pkg, outdir, redis_url="redis://localhost:6379/0"
        )
        fake = FakeSyncRedis()
        with patch(
            "osimflow.distributed_cache._get_redis_sync",
            return_value=_fake_redis_module(fake),
        ):
            campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
            # Acceptance (a): shared campaign state goes through the
            # distributed backend under the outdir-derived namespace.
            assert isinstance(campaign.cache, DistributedCache)
            assert campaign.cache._campaign_id == campaign_state_namespace(cfg.outdir)
            campaign.cache.close()


# ---------------------------------------------------------------------
# redis_url resolution: CLI flag > OSIMFLOW_REDIS_URL env var > None
# ---------------------------------------------------------------------


class TestRedisUrlResolution:
    def _args(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
        **overrides: object,
    ) -> dict[str, object]:
        args: dict[str, object] = {
            "input_variables": str(variables_yml),
            "template_sim_package": str(template_pkg),
            "n_samples": "3",
            "outdir": str(outdir),
            "openstudio_version": "3.11.0",
        }
        args.update(overrides)
        return args

    def test_default_is_none(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OSIMFLOW_REDIS_URL", raising=False)
        cfg = load_config(self._args(variables_yml, template_pkg, outdir))
        assert cfg.redis_url is None

    def test_env_var_used_when_flag_absent(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OSIMFLOW_REDIS_URL", "redis://env-host:6379/0")
        cfg = load_config(self._args(variables_yml, template_pkg, outdir))
        assert cfg.redis_url == "redis://env-host:6379/0"

    def test_flag_overrides_env_var(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OSIMFLOW_REDIS_URL", "redis://env-host:6379/0")
        cfg = load_config(
            self._args(variables_yml, template_pkg, outdir, redis_url="redis://flag-host:6379/0")
        )
        assert cfg.redis_url == "redis://flag-host:6379/0"


# ---------------------------------------------------------------------
# T8.1 lock-reproducer analog: concurrent *processes* (issue #993, c)
# ---------------------------------------------------------------------


def _mp_key(worker_idx: int, op_idx: int) -> CacheKey:
    return CacheKey(
        step="RUN_OPENSTUDIO_SIM",
        sample_id=f"s_w{worker_idx}_{op_idx}",
        openstudio_version="3.11.0",
        inputs_sha256=f"inputs-{worker_idx}-{op_idx}",
        code_sha256="code",
        container_digest="py",
        generation=0,
    )


def _mp_worker(
    storage: MutableMapping[str, str],
    db_dir: Path,
    worker_idx: int,
    n_workers: int,
    n_ops: int,
    out_base: Path,
    barrier: multiprocessing.synchronization.Barrier,
    queue: multiprocessing.Queue,  # type: ignore[type-arg]
) -> None:
    """Child-process worker for the lock-reproducer analog.

    Stores its own keys into the shared state via the distributed cache,
    waits at a barrier so all workers are storing concurrently, then
    looks up the *other* workers' keys — which can only be served via
    the shared (Redis) store because each process has its own private
    local SQLite file.
    """
    from unittest.mock import patch  # noqa: PLC0415

    from osimflow.distributed_cache import DistributedCache  # noqa: PLC0415

    errors: list[str] = []
    remote_hits = 0
    fake = FakeSyncRedis(storage=storage)
    try:
        with (
            patch(
                "osimflow.distributed_cache._get_redis_sync",
                return_value=_fake_redis_module(fake),
            ),
            patch.object(DistributedCache, "_start_subscriber"),
            patch.object(DistributedCache, "_publish"),
        ):
            cache = DistributedCache(
                db_path=db_dir / "cache.sqlite",
                redis_url="redis://fake:6379/0",
                campaign_id="mp-coordination-test",
            )
            for i in range(n_ops):
                cache.store(_mp_key(worker_idx, i), out_base / f"w{worker_idx}_{i}", exit_code=0)
            barrier.wait(timeout=60.0)
            for other in range(n_workers):
                if other == worker_idx:
                    continue
                for i in range(n_ops):
                    if cache.lookup(_mp_key(other, i)) is not None:
                        remote_hits += 1
            cache.close()
    except Exception as exc:  # noqa: BLE001 — report to parent, never crash silently
        errors.append(repr(exc))
    queue.put({"worker": worker_idx, "remote_hits": remote_hits, "errors": errors})


class TestMultiProcessStateCoordination:
    """Two-plus concurrent campaign processes against the same state.

    This is the OSimFlow analog of the T8.1 SQLite lock reproducer
    (fluxion#1790): under the distributed backend there is no shared
    SQLite file to lock — coordination happens through the (faked)
    Redis shared store, and every cross-process lookup succeeds.
    """

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork(2)")
    def test_concurrent_processes_coordinate_without_sqlite_locks(self, tmp_path: Path) -> None:
        n_workers = 3
        n_ops = 8
        db_dir = tmp_path / "work"
        db_dir.mkdir()
        out_base = tmp_path / "outs"
        out_base.mkdir()
        for w in range(n_workers):
            for i in range(n_ops):
                (out_base / f"w{w}_{i}").mkdir()

        ctx = multiprocessing.get_context("fork")
        manager = ctx.Manager()
        storage: MutableMapping[str, str] = manager.dict()
        barrier = ctx.Barrier(n_workers)
        queue: multiprocessing.Queue[dict[str, object]] = ctx.Queue()

        procs = [
            ctx.Process(
                target=_mp_worker,
                args=(storage, db_dir, w, n_workers, n_ops, out_base, barrier, queue),
                daemon=False,
            )
            for w in range(n_workers)
        ]
        try:
            for p in procs:
                p.start()
            results: list[dict[str, object]] = [queue.get(timeout=120.0) for _ in range(n_workers)]
        finally:
            for p in procs:
                p.join(timeout=30.0)
            for p in procs:
                assert p.exitcode == 0, f"worker exited {p.exitcode}"

        # Every worker saw every other worker's entries — served through
        # the shared store, not through a common SQLite file.
        expected_remote_hits = n_workers * (n_workers - 1) * n_ops
        assert sum(int(r["remote_hits"]) for r in results) == expected_remote_hits

        # No child reported errors (in particular: no sqlite lock errors,
        # no OperationalError, no exceptions at all).
        for r in results:
            assert not r["errors"], f"worker {r['worker']} reported {r['errors']}"

        # The T8.2 structural guarantee: the shared ``cache.sqlite`` file
        # was never created — each process used its own pid-private file,
        # so there was nothing to lock-contend on.
        assert not (db_dir / "cache.sqlite").exists()
        assert len(list(db_dir.glob("cache.p*.sqlite"))) == n_workers
