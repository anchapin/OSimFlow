"""Contract tests pinning the storage-endpoint docs to the code (issue #1457).

Commit 1e76213 (issue #1386) made ``_validate_storage_endpoint`` reject
plaintext HTTP for non-loopback ``--result-storage-endpoint`` /
``--s3-artifact-endpoint`` URLs unless ``--allow-insecure-storage-endpoint``
is passed (loopback hosts exempt).  Before #1457 the flag existed only in
AGENTS.md — a MinIO/R2 user behind plain HTTP hit a fail-closed validation
error at runtime with no doc path to explain or resolve it.

These tests parse the source and docs with plain regex reads (hermetic,
fast — no heavy imports):

  1. docs/user-guide.md mentions ``--allow-insecure-storage-endpoint``
  2. the user-guide storage section's endpoint-flag list matches the
     flags parsed from ``__main__.py`` and the validator in ``storage.py``
  3. the documented loopback exemption matches ``_LOOPBACK_HOSTS``
  4. the docs section cites issue #1386
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_USER_GUIDE = REPO_ROOT / "docs" / "user-guide.md"
_MAIN = REPO_ROOT / "osimflow" / "__main__.py"
_STORAGE = REPO_ROOT / "osimflow" / "storage.py"

_ADD_ARGUMENT_RE = re.compile(r'add_argument\(\s*"(-{2}[a-z0-9-]+)"')
_LOOPBACK_HOSTS_RE = re.compile(r"_LOOPBACK_HOSTS\s*=\s*frozenset\(\{([^}]*)\}\)")
_VALIDATOR_RE = re.compile(r"def _validate_storage_endpoint\(.*?(?=^\S)", re.M | re.S)
_SECTION_RE = re.compile(
    r"^#### Result storage & cost tracking$.*?(?=^#### )",
    re.M | re.S,
)
_PARAGRAPH_RE = re.compile(r"\*\*HTTPS-only storage endpoints:.*?(?=\n\n)", re.S)


def _storage_section() -> str:
    """The 'Result storage & cost tracking' section of docs/user-guide.md."""
    match = _SECTION_RE.search(_USER_GUIDE.read_text(encoding="utf-8"))
    assert match, (
        "The '#### Result storage & cost tracking' heading moved or was "
        "renamed in docs/user-guide.md; update _SECTION_RE here."
    )
    return match.group(0)


def _parse_endpoint_flags() -> set[str]:
    """The storage-endpoint flag family declared in osimflow/__main__.py."""
    flags = {
        flag
        for flag in _ADD_ARGUMENT_RE.findall(_MAIN.read_text(encoding="utf-8"))
        if "storage-endpoint" in flag or "artifact-endpoint" in flag
    }
    assert flags, (
        "No storage-endpoint flags found in osimflow/__main__.py; the CLI "
        "surface moved — update this test to parse the new location."
    )
    return flags


def _validator_body() -> str:
    """The source of _validate_storage_endpoint in osimflow/storage.py."""
    match = _VALIDATOR_RE.search(_STORAGE.read_text(encoding="utf-8"))
    assert match, (
        "_validate_storage_endpoint moved or was renamed in "
        "osimflow/storage.py; update _VALIDATOR_RE here."
    )
    return match.group(0)


def test_user_guide_documents_allow_insecure_flag() -> None:
    """The opt-out flag must be discoverable from the user guide."""
    assert "--allow-insecure-storage-endpoint" in _storage_section(), (
        "docs/user-guide.md does not document --allow-insecure-storage-"
        "endpoint (issue #1457 regression). A MinIO/R2 user behind plain "
        "HTTP hits a fail-closed error with no doc path."
    )


def test_documented_endpoint_flags_match_source() -> None:
    """The doc's endpoint flags must match the CLI + validator, both ways.

    Guards both drift directions: a flag added to or removed from
    ``__main__.py`` without a doc update, and a doc mention of a flag the
    CLI no longer declares.
    """
    section = _storage_section()
    documented = {
        flag
        for flag in re.findall(r"(--[a-z0-9-]+)", section)
        if "storage-endpoint" in flag or "artifact-endpoint" in flag
    }
    declared = _parse_endpoint_flags()
    assert documented == declared, (
        f"docs/user-guide.md endpoint flags {sorted(documented)} disagree "
        f"with osimflow/__main__.py {sorted(declared)}; update the 'Result "
        "storage & cost tracking' section."
    )
    validator = _validator_body()
    assert "--allow-insecure-storage-endpoint" in validator, (
        "_validate_storage_endpoint no longer names the opt-out flag in "
        "osimflow/storage.py; update the validator and the docs together."
    )
    assert "--allow-insecure-storage-endpoint" in declared, (
        "osimflow/__main__.py no longer declares "
        "--allow-insecure-storage-endpoint but osimflow/storage.py still "
        "enforces it — the opt-out has no CLI surface."
    )


def test_documented_loopback_hosts_match_source() -> None:
    """The doc's loopback exemption must match _LOOPBACK_HOSTS exactly.

    Guards both drift directions: a host added to or removed from the
    ``_LOOPBACK_HOSTS`` frozenset without a doc update, and a doc-listed
    host the code no longer exempts.
    """
    hosts_match = _LOOPBACK_HOSTS_RE.search(_STORAGE.read_text(encoding="utf-8"))
    assert hosts_match, (
        "_LOOPBACK_HOSTS moved or changed shape in osimflow/storage.py; "
        "update _LOOPBACK_HOSTS_RE here."
    )
    hosts = set(re.findall(r'"([^"]+)"', hosts_match.group(1)))
    section = _storage_section()
    missing = {host for host in hosts if f"`{host}`" not in section}
    assert not missing, (
        f"docs/user-guide.md omits loopback hosts {sorted(missing)}; the "
        "exemption list drifted from osimflow/storage.py _LOOPBACK_HOSTS."
    )
    paragraph_match = _PARAGRAPH_RE.search(section)
    assert paragraph_match, (
        "The '**HTTPS-only storage endpoints:**' paragraph moved in "
        "docs/user-guide.md; update _PARAGRAPH_RE here."
    )
    doc_hosts = {
        token
        for token in re.findall(r"`([^`]+)`", paragraph_match.group(0))
        if re.fullmatch(r"[0-9a-z.:]+", token)
    }
    assert doc_hosts == hosts, (
        f"docs/user-guide.md lists loopback hosts {sorted(doc_hosts)} but "
        f"osimflow/storage.py exempts {sorted(hosts)}; align the two."
    )


def test_doc_and_validator_cite_issue_1386() -> None:
    """The docs section must cite issue #1386 as the origin of the rule."""
    assert "#1386" in _storage_section(), (
        "docs/user-guide.md no longer cites issue #1386 for the https-only "
        "storage endpoint rule (issue #1457 acceptance criterion)."
    )
    assert "#1386" in _validator_body(), (
        "_validate_storage_endpoint no longer cites issue #1386; the docs citation tracks the code."
    )
