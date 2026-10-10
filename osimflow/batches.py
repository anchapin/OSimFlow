"""Sequential multi-batch driver (issue #1874).

Mirrors openstudio-server's ``rake execute_sequential``: a manifest lists N
named batches; each runs as its own ``osimflow run`` campaign, in order, under
an exclusive submit lock so concurrent drivers never collide. Each campaign
lands in ``<root>/Batch<id>_<name>`` (stable, so ``osimflow status|download
--from-s3`` can address it) and a per-batch summary is written to
``<root>/batches_summary.json``.

Manifest (YAML or JSON)::

    common:                     # flags shared by every batch
      executor: aws_batch
      detach-s3: true
      result-storage-bucket: my-bucket
    batches:
      - {id: 1, name: baseline, n_samples: 10, input_variables: a.yml}

Keys are ``osimflow run`` flag names without the leading ``--`` (underscores
and dashes are used exactly as the flag spells them); ``true`` emits the bare
flag, ``false``/``null`` omits it, lists expand to repeated values.
"""

import argparse
import fcntl
import json
import logging
import re
import subprocess  # nosec
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import OSimFlowValueError

log = logging.getLogger(__name__)

SUMMARY_NAME = "batches_summary.json"
LOCK_NAME = ".submit.lock"
_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class BatchManifestError(OSimFlowValueError):
    """Raised for an invalid batch manifest."""


class SubmitLockTimeout(TimeoutError):
    """Raised when the submit lock could not be acquired in time."""


@dataclass
class BatchResult:
    """Outcome of one batch."""

    batch_id: str
    name: str
    campaign: str
    outdir: str
    status: str  # success | failed | skipped | dry-run
    returncode: int | None
    elapsed_s: float


def batch_campaign_name(batch_id: object, name: str) -> str:
    """Stable campaign id ``Batch<id>_<name>`` (filesystem/S3 safe)."""
    return f"Batch{_NAME_RE.sub('-', str(batch_id))}_{_NAME_RE.sub('-', name)}"


def load_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse *path*; return ``(common, batches)`` validating ids/names."""
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise BatchManifestError(f"cannot read batch manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("batches"), list):
        raise BatchManifestError("manifest must be a mapping with a 'batches' list")
    common = data.get("common") or {}
    if not isinstance(common, dict):
        raise BatchManifestError("'common' must be a mapping")
    batches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(data["batches"]):
        if not isinstance(entry, dict) or "id" not in entry or "name" not in entry:
            raise BatchManifestError(f"batches[{i}] needs 'id' and 'name'")
        cname = batch_campaign_name(entry["id"], str(entry["name"]))
        if cname in seen:
            raise BatchManifestError(f"duplicate batch {cname!r}")
        seen.add(cname)
        batches.append(entry)
    if not batches:
        raise BatchManifestError("manifest has no batches")
    return common, batches


def _run_actions() -> dict[str, argparse.Action]:
    """Map every ``osimflow run`` option string to its argparse action."""
    from .__main__ import _add_run_args  # noqa: PLC0415

    run = argparse.ArgumentParser().add_subparsers().add_parser("run")
    _add_run_args(run)
    return {opt: act for act in run._actions for opt in act.option_strings}  # noqa: SLF001


def build_run_argv(common: dict[str, Any], entry: dict[str, Any], outdir: Path) -> list[str]:
    """``osimflow run`` argv for one batch (entry overrides common).

    Keys are validated against the real ``run`` parser: unknown flags raise
    :class:`BatchManifestError`; ``append`` flags repeat per list item;
    ``BooleanOptionalAction`` flags emit ``--no-x`` for ``false``.
    """
    actions = _run_actions()
    merged = {**common, **{k: v for k, v in entry.items() if k not in ("id", "name")}}
    merged.pop("outdir", None)
    argv = [sys.executable, "-m", "osimflow", "run"]
    for key, value in merged.items():
        flag = f"--{str(key).lstrip('-')}"
        action = actions.get(flag)
        if action is None:
            raise BatchManifestError(f"unknown osimflow run flag {flag!r} in manifest")
        if isinstance(action, argparse.BooleanOptionalAction):
            if value is not None:
                argv.append(flag if value else f"--no-{flag[2:]}")
            continue
        if value is None or value is False:
            continue
        if value is True:
            argv.append(flag)
        elif isinstance(value, list):
            if isinstance(action, argparse._AppendAction):  # noqa: SLF001
                for v in value:
                    argv.extend([flag, str(v)])
            else:
                argv.append(flag)
                argv.extend(str(v) for v in value)
        else:
            argv.extend([flag, str(value)])
    argv.extend(["--outdir", str(outdir)])
    return argv


@contextmanager
def submit_lock(root: Path, timeout_s: float = 3600.0, poll_s: float = 0.5) -> Iterator[None]:
    """Exclusive advisory lock on ``<root>/.submit.lock`` (flock)."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_NAME).open("w") as fh:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SubmitLockTimeout(
                        f"another driver holds {root / LOCK_NAME} (waited {timeout_s:.0f}s)"
                    ) from None
                time.sleep(poll_s)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def run_batches(
    manifest: Path,
    root: Path,
    *,
    continue_on_error: bool = False,
    dry_run: bool = False,
    lock_timeout_s: float = 3600.0,
) -> list[BatchResult]:
    """Run every batch in order; write and return the per-batch summary."""
    common, batches = load_manifest(manifest)
    for entry in batches:  # validate every batch before launching any child
        build_run_argv(common, entry, root / batch_campaign_name(entry["id"], str(entry["name"])))
    if dry_run:
        return _execute(common, batches, root, continue_on_error, True)
    with submit_lock(root, lock_timeout_s):
        return _execute(common, batches, root, continue_on_error, False)


def _execute(
    common: dict[str, Any],
    batches: list[dict[str, Any]],
    root: Path,
    continue_on_error: bool,
    dry_run: bool,
) -> list[BatchResult]:
    results: list[BatchResult] = []
    failed = False
    for entry in batches:
        cname = batch_campaign_name(entry["id"], str(entry["name"]))
        outdir = root / cname
        if failed and not continue_on_error:
            results.append(
                BatchResult(
                    str(entry["id"]), str(entry["name"]), cname, str(outdir), "skipped", None, 0.0
                )
            )
            continue
        argv = build_run_argv(common, entry, outdir)
        if dry_run:
            log.info("dry-run %s: %s", cname, " ".join(argv))
            print(" ".join(argv))
            results.append(
                BatchResult(
                    str(entry["id"]), str(entry["name"]), cname, str(outdir), "dry-run", None, 0.0
                )
            )
            continue
        t0 = time.monotonic()
        proc = subprocess.run(argv, check=False)  # nosec  # noqa: S603
        ok = proc.returncode == 0
        failed = failed or not ok
        if not ok:
            log.error("batch %s failed with exit code %s", cname, proc.returncode)
        results.append(
            BatchResult(
                str(entry["id"]),
                str(entry["name"]),
                cname,
                str(outdir),
                "success" if ok else "failed",
                proc.returncode,
                time.monotonic() - t0,
            )
        )
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        (root / SUMMARY_NAME).write_text(json.dumps([asdict(r) for r in results], indent=2))
    return results
