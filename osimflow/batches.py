"""Sequential multi-batch driver (issue #1874).

A general multi-batch sequential campaign driver: a manifest lists N
named batches; each runs as its own ``osimflow run`` campaign, in order, under
an exclusive submit lock so concurrent drivers never collide. Each campaign
lands in ``<root>/<campaign>`` (stable, so ``osimflow status|download
--from-s3`` can address it) and a per-batch summary is written to
``<root>/batches_summary.json``. The campaign id is the batch's explicit
``campaign`` key, else ``common.name_template`` rendered with ``{id}`` /
``{name}`` (default ``{name}``; e.g. ``Batch{id}_{name}``).

Manifest (YAML or JSON)::

    common:                     # flags shared by every batch
      executor: aws_batch
      detach-s3: true
      result-storage-bucket: my-bucket
      name_template: "{name}"   # optional; consumed by the driver, not a run flag
    batches:
      - {id: 1, name: baseline, n_samples: 10, input_variables: a.yml}
      - {name: retrofit, campaign: retrofit-v2, n_samples: 20}

Keys are ``osimflow run`` flag names without the leading ``--`` (underscores
and dashes are used exactly as the flag spells them); ``true`` emits the bare
flag, ``false``/``null`` omits it, lists expand to repeated values.
"""

import argparse

try:
    import fcntl
except ImportError:  # Windows: POSIX-only module
    fcntl = None  # type: ignore[assignment]
import json
import logging
import re
import string
import subprocess  # nosec
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import OSimFlowError, OSimFlowValueError

log = logging.getLogger(__name__)

SUMMARY_NAME = "batches_summary.json"
LOCK_NAME = ".submit.lock"
_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class BatchManifestError(OSimFlowValueError):
    """Raised for an invalid batch manifest."""


class SubmitLockTimeout(OSimFlowError, TimeoutError):
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


DEFAULT_NAME_TEMPLATE = "{name}"
_DRIVER_KEYS = ("id", "name", "campaign", "name_template")


def batch_campaign_name(batch_id: object, name: str, template: str = DEFAULT_NAME_TEMPLATE) -> str:
    """Stable campaign id (filesystem/S3 safe) from *template* (``{id}``, ``{name}``)."""
    for _lit, field, spec, conv in string.Formatter().parse(template):
        if field is not None and (field not in ("id", "name") or spec or conv):
            raise BatchManifestError(
                f"invalid name_template {template!r}: only plain {{id}} and {{name}} are allowed"
            )
    try:
        rendered = template.format(
            id=_NAME_RE.sub("-", str(batch_id)), name=_NAME_RE.sub("-", name)
        )
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        raise BatchManifestError(
            f"invalid name_template {template!r} (use only {{id}} and {{name}}): {exc}"
        ) from exc
    cname = _NAME_RE.sub("-", rendered).strip(".-")
    if not cname:
        raise BatchManifestError(f"name_template {template!r} produced an empty campaign name")
    return cname


def load_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse *path*; return ``(common, batches)`` validating ids/names.

    Each returned entry carries a resolved, unique ``campaign`` id. ``id``
    defaults to the 1-based position and ``name`` to ``campaign`` when omitted.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise BatchManifestError(f"cannot read batch manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("batches"), list):
        raise BatchManifestError("manifest must be a mapping with a 'batches' list")
    common = data.get("common") or {}
    if not isinstance(common, dict):
        raise BatchManifestError("'common' must be a mapping")
    template = common.get("name_template", DEFAULT_NAME_TEMPLATE)
    if not isinstance(template, str) or not template:
        raise BatchManifestError("'name_template' must be a non-empty string")
    batches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(data["batches"]):
        if not isinstance(raw, dict) or ("name" not in raw and "campaign" not in raw):
            raise BatchManifestError(f"batches[{i}] needs 'name' (or 'campaign')")
        for key in ("name", "campaign"):
            if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
                raise BatchManifestError(f"batches[{i}].{key} must be a non-empty string")
        entry = dict(raw)
        entry.setdefault("id", i + 1)
        entry.setdefault("name", entry.get("campaign"))
        if "campaign" in entry:
            cname = batch_campaign_name(entry["id"], str(entry["campaign"]), "{name}")
        else:
            cname = batch_campaign_name(entry["id"], str(entry["name"]), template)
        # casefold: campaign dirs collide on case-insensitive filesystems
        if cname.casefold() in seen:
            raise BatchManifestError(f"duplicate batch {cname!r}")
        seen.add(cname.casefold())
        entry["campaign"] = cname
        batches.append(entry)
    if not batches:
        raise BatchManifestError("manifest has no batches")
    return common, batches


def _run_actions() -> dict[str, argparse.Action]:
    """Map every ``osimflow run`` option string to its argparse action."""
    from .cli.parser import _add_run_args  # noqa: PLC0415

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
    merged = {
        **{k: v for k, v in common.items() if k not in _DRIVER_KEYS},
        **{k: v for k, v in entry.items() if k not in _DRIVER_KEYS},
    }
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
                if fcntl is not None:
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
            if fcntl is not None:
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
        build_run_argv(common, entry, root / entry["campaign"])
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
        cname = entry["campaign"]
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
            sys.stdout.write(" ".join(argv) + "\n")
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
