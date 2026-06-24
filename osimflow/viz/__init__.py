"""Local ephemeral dashboard for OSimFlow campaign results (issue #199).

Provides an optional Streamlit-based web UI for interactive exploration
of campaign outputs (aggregated_results.csv, run.json). Requires
``pip install osimflow[viz]``; otherwise the ``osimflow dashboard``
subcommand prints a clear installation hint.
"""

from osimflow.viz.dashboard import create_dashboard_app

__all__ = ["create_dashboard_app"]
