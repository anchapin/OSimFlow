from unittest import mock

from osimflow import measure_resolver as mr


def test_unlisted_gem_refused(monkeypatch):
    monkeypatch.delenv("OSIMFLOW_GEM_ALLOWLIST", raising=False)
    with mock.patch.object(mr.subprocess, "run") as run:
        assert mr._gem_install("evil-gem") is False
    run.assert_not_called()


def test_invalid_name_refused(monkeypatch):
    monkeypatch.setenv("OSIMFLOW_GEM_ALLOWLIST", "--foo")
    with mock.patch.object(mr.subprocess, "run") as run:
        assert mr._gem_install("--foo") is False
    run.assert_not_called()


def test_allowlisted_gem_pinned_source(monkeypatch):
    monkeypatch.setenv("OSIMFLOW_GEM_ALLOWLIST", "mygem")
    with mock.patch.object(mr.subprocess, "run") as run:
        run.return_value.returncode = 0
        assert mr._gem_install("mygem") is True
    cmd = run.call_args[0][0]
    assert cmd[-1] == "mygem" and mr._GEM_SOURCE in cmd
