"""Per-executor configuration modules (issue #1575).

Each built-in executor owns its configuration surface in a dedicated
module named after its ``ExecutorRegistry`` key:

* the executor's ``XConfig`` dataclass (where one exists — ``local``,
  ``slurm``, ``aws_batch``, ``azure_batch``, ``google_batch``,
  ``nomad``), composed by ``CampaignConfig`` in ``osimflow/config.py``
  exactly as before #1575, and
* an ``add_arguments(parser_group)`` hook registering the executor's
  ``--flags`` on the ``run`` / ``warm-cache`` subparser with the same
  names, defaults, and help the monolithic tree used pre-#1575.

``osimflow.__main__._add_run_args`` calls
:func:`add_executor_arguments` instead of hand-coding the executor
flag tree; ``osimflow.config`` re-exports every ``XConfig`` so
``from osimflow.config import SlurmConfig`` keeps working.

Third-party executor plug-ins participate through the same hook
registry (see :mod:`osimflow.executor_configs.base`): either call
:func:`register_executor_arguments` from the plug-in package, or
define an ``add_arguments`` staticmethod on the executor class —
``ExecutorRegistry.discover_plugins`` auto-registers it alongside the
class.

This package is deliberately a leaf (stdlib-only imports) so
``osimflow.config`` can compose the configs without pulling the
executor implementations — and their SDK imports — into its import
graph.
"""

from osimflow.executor_configs import (  # noqa: F401 — re-exported modules
    aws_batch,
    azure_batch,
    dask_jobqueue,
    docker_swarm,
    google_batch,
    kubernetes,
    local,
    nomad,
    pbs,
    slurm,
)
from osimflow.executor_configs.aws_batch import AWSBatchConfig
from osimflow.executor_configs.azure_batch import AzureBatchConfig
from osimflow.executor_configs.base import (
    ExecutorArgumentHook,
    add_executor_arguments,
    iter_executor_argument_hooks,
    register_executor_arguments,
)
from osimflow.executor_configs.google_batch import GoogleBatchConfig
from osimflow.executor_configs.local import LocalConfig
from osimflow.executor_configs.nomad import NomadConfig
from osimflow.executor_configs.slurm import SlurmConfig

__all__ = [
    "AWSBatchConfig",
    "AzureBatchConfig",
    "ExecutorArgumentHook",
    "GoogleBatchConfig",
    "LocalConfig",
    "NomadConfig",
    "SlurmConfig",
    "add_executor_arguments",
    "iter_executor_argument_hooks",
    "register_executor_arguments",
]


# ======================================================================
# Register built-in executor argument hooks
# ======================================================================
# Order does not matter — ``iter_executor_argument_hooks`` iterates in
# sorted executor-name order so parser construction (and --help output)
# stays deterministic.
register_executor_arguments("aws_batch", aws_batch.add_arguments)
register_executor_arguments("azure_batch", azure_batch.add_arguments)
register_executor_arguments("dask_jobqueue", dask_jobqueue.add_arguments)
register_executor_arguments("docker_swarm", docker_swarm.add_arguments)
register_executor_arguments("google_batch", google_batch.add_arguments)
register_executor_arguments("kubernetes", kubernetes.add_arguments)
register_executor_arguments("local", local.add_arguments)
register_executor_arguments("nomad", nomad.add_arguments)
register_executor_arguments("pbs", pbs.add_arguments)
register_executor_arguments("slurm", slurm.add_arguments)
