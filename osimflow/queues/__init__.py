"""Queue subsystems grouped by their ADR-0005 roles (issue #1891).

* :mod:`osimflow.queues.dispatch` — work dispatch (was ``taskqueue``).
* :mod:`osimflow.queues.journal` — filesystem crash-recovery journal (was ``jobqueue``).
* :mod:`osimflow.queues.control_plane` — Redis control-plane broadcast (was ``distributed_jobqueue``).

The old flat module names are ``sys.modules`` aliases of these.
"""
