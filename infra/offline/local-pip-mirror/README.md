# local-pip-mirror/

This directory is reserved for a local pip mirror setup using
[pip2pi](https://github.com/woidzero/pip2pi) or a similar tool.

## Quick start

```bash
# On the online machine — build the mirror from the offline pip directory
pip install pip2pi
pip2pi --link /path/to/local-pip-mirror/ /path/to/offline/pip/

# Serve the mirror
cd local-pip-mirror/
python -m http.server 8080 &

# On air-gapped machines, configure pip.conf:
# [global]
# extra-index-url = http://pip-mirror.internal:8080/simple/
# find-links = http://pip-mirror.internal:8080/simple/
```

## Notes

- `pip2pi` creates a `/simple/` directory structure compatible with pip.
- The mirror can be served over NFS or an internal HTTP server.
- For large teams, consider running [PyPI Mirror](https://pypi.org/project/pypimirror/)
  which implements the full PyPI API.
