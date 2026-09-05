"""Cache-key portability characterization tests (issue #1558).

The Campaign's per-step cache key is content-addressed. Before this
fix, ``sha256_of_files`` (osimflow/cache.py) embedded the absolute
path of every top-level input (``str(p).encode()``) and the
host-OS-separated relative path of every directory descendant. The
same file tree rooted at two different absolute locations therefore
produced different cache keys and forced a full cold replay on
resume-by-replay.

These tests pin the relocation-stable, separator-stable contract that
``sha256_of_files`` must satisfy from this point forward:

* (a) Hashing identical file trees rooted at two different
      directories yields equal hashes (relocation-stable).
* (b) The hash is OS-separator-stable — forward-slash-only paths and
      the actual host separator produce identical hashes.
* (c) The Campaign-level cache lookup against a *moved* template
      package + variables.yml is a HIT (resume-by-replay survives
      relocation).
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

import pytest

from osimflow.cache import CacheKey, SQLiteCache, sha256_of_files


def _make_tree(root: Path, *, name: str = "tree") -> Path:
    """Build a small but representative file tree at ``root``.

    Layout::

        <root>/<name>/
            foo.txt            (file)
            bar.txt            (file)
            nested/
                deep.txt      (file)
    """
    tree = root / name
    nested = tree / "nested"
    nested.mkdir(parents=True)
    (tree / "foo.txt").write_text("FOO-CONTENT")
    (tree / "bar.txt").write_text("BAR-CONTENT")
    (nested / "deep.txt").write_text("DEEP-CONTENT")
    return tree


# ---------------------------------------------------------------------------
# (a) Relocation stability: same tree, two roots → same hash
# ---------------------------------------------------------------------------
def test_identical_tree_at_two_roots_hashes_equal(tmp_path: Path) -> None:
    """Two copies of an identical tree at different absolute roots
    must produce the same ``sha256_of_files`` output."""
    tree_a = _make_tree(tmp_path / "root_a")
    tree_b = _make_tree(tmp_path / "root_b")
    assert tree_a != tree_b
    # Sanity: the trees are byte-identical at the file level.
    files_a = sorted(p.relative_to(tree_a).as_posix() for p in tree_a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(tree_b).as_posix() for p in tree_b.rglob("*") if p.is_file())
    contents_a = [p.read_bytes() for p in sorted(tree_a.rglob("*")) if p.is_file()]
    contents_b = [p.read_bytes() for p in sorted(tree_b.rglob("*")) if p.is_file()]
    assert files_a == files_b
    assert contents_a == contents_b
    # The directory contents are byte-identical.
    h_a = sha256_of_files([tree_a])
    h_b = sha256_of_files([tree_b])
    assert h_a == h_b, f"sha256_of_files must be relocation-stable. root_a={h_a!r} root_b={h_b!r}"


def test_single_file_at_two_paths_hashes_equal(tmp_path: Path) -> None:
    """A single file moved to two absolute paths must hash equal."""
    src = tmp_path / "src" / "variables.yml"
    src.parent.mkdir(parents=True)
    src.write_text("a: 1\nb: 2\n")

    dst = tmp_path / "dst" / "variables.yml"
    dst.parent.mkdir(parents=True)
    shutil.copy2(src, dst)

    h_src = sha256_of_files([src])
    h_dst = sha256_of_files([dst])
    assert h_src == h_dst


def test_rglob_flat_list_at_two_roots_hashes_equal(tmp_path: Path) -> None:
    """The campaign.py:2825 caller passes the result of
    ``rglob('*')`` (a flat list of files + intermediate directories)
    into ``sha256_of_files``. The hash must be relocation-stable
    for that pattern too."""
    tree_a = _make_tree(tmp_path / "ka")
    tree_b = _make_tree(tmp_path / "kb")

    h_a = sha256_of_files(sorted(tree_a.rglob("*")))
    h_b = sha256_of_files(sorted(tree_b.rglob("*")))
    assert h_a == h_b


def test_content_change_breaks_relocation_stable_hash(tmp_path: Path) -> None:
    """Relocation stability must NOT collapse different content into
    the same hash. A single byte change at the new location must
    still invalidate the cache key."""
    tree_a = _make_tree(tmp_path / "alpha")
    tree_b = _make_tree(tmp_path / "beta")
    h_before = sha256_of_files([tree_a])

    tree_b.joinpath("foo.txt").write_text("FOO-CONTENT-MUTATED")
    h_after = sha256_of_files([tree_b])
    assert h_before != h_after


# ---------------------------------------------------------------------------
# (b) Separator stability: forward-slash paths produce the same hash
# ---------------------------------------------------------------------------
def test_forward_slash_string_and_path_object_hash_equal(tmp_path: Path) -> None:
    """Constructing a ``Path`` from a forward-slash string and hashing
    it must produce the same hash as constructing it from the same
    forward-slash string at a different absolute prefix. Pure
    forward-slash normalization must hold regardless of host OS."""
    tree_a = _make_tree(tmp_path / "forward_a")
    tree_b = _make_tree(tmp_path / "forward_b")

    # Use ``PurePosixPath`` to make the forward-slash form explicit.
    # On POSIX hosts this is the same as ``Path``; the assertion is
    # what matters.
    posix_a = PurePosixPath(str(tree_a))
    posix_b = PurePosixPath(str(tree_b))

    h_a = sha256_of_files([Path(posix_a)])
    h_b = sha256_of_files([Path(posix_b)])
    assert h_a == h_b


def test_basename_collapses_to_stable_identifier(tmp_path: Path) -> None:
    """Top-level file inputs contribute ``Path(p).name`` rather than
    the full absolute path. Two relocations of the same file must
    therefore hash equal at the ``Path.name`` level."""
    src = tmp_path / "x" / "custom_apply.py"
    src.parent.mkdir(parents=True)
    src.write_text("def apply(...): pass\n")

    moved = tmp_path / "y" / "deep" / "nested" / "custom_apply.py"
    moved.parent.mkdir(parents=True)
    shutil.copy2(src, moved)

    h_src = sha256_of_files([src])
    h_moved = sha256_of_files([moved])
    assert h_src == h_moved, (
        "A top-level file input must hash by basename + content, not by absolute path."
    )


# ---------------------------------------------------------------------------
# (c) Integration: campaign-level cache hit after relocating inputs
# ---------------------------------------------------------------------------
@pytest.fixture
def cache_db(tmp_path: Path) -> SQLiteCache:
    return SQLiteCache(tmp_path / "cache.sqlite")


def test_relocated_inputs_produce_matching_inputs_hash(
    tmp_path: Path, cache_db: SQLiteCache
) -> None:
    """The integration counterpart to (a): build a ``CacheKey`` whose
    ``inputs_sha256`` is the ``sha256_of_files`` of an inputs file,
    copy the inputs file to a new absolute location, recompute the
    hash at the new location, and assert they match (cache hit)."""
    variables_a = tmp_path / "orig" / "variables.yml"
    variables_a.parent.mkdir(parents=True)
    variables_a.write_text("n_samples: 3\n")

    h_a = sha256_of_files([variables_a])
    key_a = CacheKey(
        step="GENERATE_LHS_SAMPLES",
        sample_id="ALL",
        openstudio_version="N/A",
        inputs_sha256=h_a,
        code_sha256="code",
        container_digest="img",
        generation=0,
    )
    out = tmp_path / "samples.json"
    out.write_text('{"samples": []}')
    cache_db.store(key_a, out, exit_code=0)
    assert cache_db.lookup(key_a) is not None

    # Relocate the inputs file.
    variables_b = tmp_path / "moved" / "elsewhere" / "variables.yml"
    variables_b.parent.mkdir(parents=True)
    shutil.copy2(variables_a, variables_b)

    h_b = sha256_of_files([variables_b])
    assert h_a == h_b, "Hash of relocated variables.yml must equal the original"

    key_b = CacheKey(
        step="GENERATE_LHS_SAMPLES",
        sample_id="ALL",
        openstudio_version="N/A",
        inputs_sha256=h_b,
        code_sha256="code",
        container_digest="img",
        generation=0,
    )
    # The cache entry built at the original location must be
    # retrievable using the cache key computed at the relocated
    # location — this is the resume-by-replay contract.
    assert cache_db.lookup(key_b) is not None, (
        "Cache lookup must hit after the inputs file is relocated."
    )


def test_relocated_template_sim_package_produces_matching_inputs_hash(
    tmp_path: Path,
) -> None:
    """The ``PREFLIGHT_RUN_MODEL`` caller (campaign.py:2825) hashes
    ``sorted(template_sim_package.rglob('*'))``. Relocating the
    template package directory must not change the hash."""
    pkg_a = _make_tree(tmp_path / "orig_template", name="template")
    pkg_b_parent = tmp_path / "new_location"
    pkg_b = shutil.copytree(pkg_a, pkg_b_parent / "template")

    h_a = sha256_of_files(sorted(pkg_a.rglob("*")))
    h_b = sha256_of_files(sorted(pkg_b.rglob("*")))
    assert h_a == h_b, (
        "Hash of a relocated template_sim_package must equal the "
        "original — same content tree, different absolute root."
    )
