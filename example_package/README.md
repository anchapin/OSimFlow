# `example_package/`

Minimal simulation package used as the default `--template_sim_package`
for the performance bench (issue #10) and the README quickstart. The
files use the test-mode JSON convention documented in
[`osimflow/apply_params.py`](../osimflow/apply_params.py) — i.e. the
`.osm` content is a JSON object (not the production binary/XML format),
so the bench runs on hosts that don't have the OpenStudio Python
bindings installed.

The default attribute values match the `variables.yml` distributions
shipped at the repo root (uniform `window_u_value`, lognormal
`infiltration_rate`, uniform `hvac_setpoint`). Edit the JSON to taste;
the pre-flight check in `bin/apply_params_to_model.py` will fail loudly
if a new `variables.yml` variable is missing from the attributes map.
