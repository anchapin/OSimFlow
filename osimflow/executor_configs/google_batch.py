"""Google Cloud Batch executor configuration + CLI flags (issue #1575).

Owns the ``GoogleBatchConfig`` dataclass and the ``--google-*`` flags
that ``osimflow run``/``osimflow warm-cache`` register for the
``google_batch`` executor. Registered as the ``google_batch`` argument
hook by ``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses


@dataclasses.dataclass(frozen=True)
class GoogleBatchConfig:
    """Google Cloud Batch executor configuration.

    Attributes
    ----------
    project_id
        Google Cloud project ID.
    region
        Google Cloud region.
    service_account
        Service account email for the job.
    use_spot
        Whether to use Spot/Preemptible instances.
    fallback_to_on_demand
        Whether to fall back to on-demand instances when Spot is unavailable.
    max_retries
        Maximum number of retries for failed jobs.
    payload_secret_name
        Secret Manager secret name holding the task-payload HMAC
        secret. When set, the job spec emits
        ``environment.secret_variables`` instead of a literal env
        value (issue #1633); the Batch agent resolves the secret via
        the job's service account.
    """

    project_id: str | None = None
    region: str = "us-central1"
    service_account: str | None = None
    use_spot: bool = False
    fallback_to_on_demand: bool = False
    max_retries: int = 3
    payload_secret_name: str | None = None


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``google_batch`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument(
        "--google-batch-project-id",
        default=None,
        help="Google Cloud project ID (e.g. my-project).",
    )
    parser_group.add_argument(
        "--google-batch-region",
        default="us-central1",
        help="Google Cloud region for Batch jobs (default: us-central1).",
    )
    parser_group.add_argument(
        "--google-batch-service-account",
        default=None,
        help="Google Cloud service account email for Batch jobs.",
    )
    parser_group.add_argument(
        "--google-use-spot",
        action="store_true",
        help=(
            "Use Google Spot VMs (preemptible) for Batch jobs (issue #352). "
            "When a preemptible VM is interrupted, the executor retries up to "
            "--google-max-retries times before falling back to on-demand "
            "(if --google-fallback-to-on-demand is set) or failing."
        ),
    )
    parser_group.add_argument(
        "--google-fallback-to-on-demand",
        action="store_true",
        help=(
            "When Google Spot/preemptible retries are exhausted, fall back to "
            "on-demand VMs instead of failing (issue #352). "
            "Requires --google-use-spot."
        ),
    )
    parser_group.add_argument(
        "--google-max-retries",
        type=int,
        default=3,
        help=(
            "Maximum number of retries on Google preemptible VM interruption "
            "(default: 3). Each retry uses exponential backoff. After "
            "exhausting retries, the job fails unless "
            "--google-fallback-to-on-demand is set."
        ),
    )
    parser_group.add_argument(
        "--google-batch-payload-secret-name",
        default=None,
        help=(
            "Secret Manager secret name (e.g. osimflow-payload-hmac) "
            "holding the task-payload HMAC secret. When set, the job "
            "spec emits environment.secret_variables instead of a "
            "literal env value, so the raw secret never appears in "
            "the job spec where it is readable via 'gcloud batch jobs "
            "describe' and persists in job history (issue #1633). The "
            "Batch agent resolves the secret at task start via the "
            "job's service account (--google-batch-service-account), "
            "which needs roles/secretmanager.secretAccessor on it. "
            "Requires OSIMFLOW_TASK_PAYLOAD_SECRET on the orchestrator "
            "with the same value for signing. See docs/secret-management.md."
        ),
    )
