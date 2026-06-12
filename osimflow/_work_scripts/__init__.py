"""Work scripts shipped with the osimflow package.

These scripts are the default implementations of the per-step work
functions called by :mod:`osimflow.work`.  They are packaged inside
``osimflow`` so that ``pip install`` delivers a working wheel without
requiring a checkout of the repository.

The scripts are invoked as subprocesses (so the BYOS contract stays
identical: same argv, same exit code, same logging format).  The
resolver in :func:`osimflow.work._resolve_work_script` locates them
via ``importlib.resources`` when running from a wheel, or falls back
to the repo ``bin/`` directory during development.
"""
