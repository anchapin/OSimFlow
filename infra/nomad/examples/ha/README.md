# OSimFlow Nomad HA cluster — native host-OS deployment (issue #619)

This directory contains the **native** production-grade Nomad HA recipe for
OSimFlow. The Nomad control plane (servers and client agents) runs directly
on the host OS — on bare metal or VMs — **not** inside containers.

> **Deprecation notice (issue #619).** The previous `hind`
> (Hashistack-in-Docker) template — a `docker-compose.yml` that ran the
> Nomad servers and client agents inside Docker containers, with the Docker
> socket bind-mounted so those containers could launch further containers —
> has been removed. OpenStudio simulations are highly compute-intensive, and
> wrapping the cluster in Docker-in-Docker abstractions introduced
> significant, unnecessary performance overhead and a convoluted trust
> boundary. The native topology maximises bare-metal / VM compute efficiency
> and relies on strict ACL policies plus native mTLS for isolation instead
> of nested containerization. See
> [`docs/nomad-production.md`](../../../docs/nomad-production.md) for the
> full rationale, topology, and security model.

## Topology

```
3 server nodes  (native `nomad agent`, Raft quorum, tolerates 1 failure)
N client nodes  (native `nomad agent`, unprivileged Docker task driver)
```

Only the OpenStudio **workloads** run in containers — via Nomad's Docker
task driver with `privileged = false` (see `../../osimflow_worker.hcl`).
The agents that schedule them do not.

## Files

| File | Purpose |
|---|---|
| `server.hcl` | Server config template (shared by all 3 nodes; per-node identity via `-node`/`-bind` flags) |
| `client.hcl` | Client config (unprivileged Docker task driver, ACL, mTLS template) |
| `bootstrap.sh` | ACL bootstrap + policy/token registration (cluster-agnostic; curls `NOMAD_ADDR`) |

The ACL policies themselves live in `../acl/policies/` (`agent.hcl`,
`worker.hcl`) and are unchanged — they are reusable across deployment styles.

## Bring-up (3-server quorum)

1. **Edit the join list.** In `server.hcl` and `client.hcl`, replace the
   placeholder `retry_join` IPs (`10.0.0.11/12/13`) with the real addresses
   of your three server nodes (or use a cluster DNS name / cloud auto-join).

2. **Start the servers** (one command per node, on each server host):

   ```bash
   # node 1
   nomad agent -config=server.hcl -node=nomad-server-1 -bind=10.0.0.11
   # node 2
   nomad agent -config=server.hcl -node=nomad-server-2 -bind=10.0.0.12
   # node 3
   nomad agent -config=server.hcl -node=nomad-server-3 -bind=10.0.0.13
   ```

   In production, run each as a systemd unit (`systemctl enable --now nomad`)
   rather than a foreground process. A minimal unit:

   ```ini
   [Unit]
   Description=Nomad Server
   After=network-online.target
   Wants=network-online.target

   [Service]
   ExecStart=/usr/bin/nomad agent -config=/etc/nomad/server.hcl
   Restart=on-failure
   User=nomad
   Group=nomad

   [Install]
   WantedBy=multi-user.target
   ```

3. **Start the clients** on each compute node:

   ```bash
   nomad agent -config=client.hcl -node=<this-node> -bind=<this-node-ip>
   ```

4. **Bootstrap ACLs and policies:**

   ```bash
   export NOMAD_ADDR=http://10.0.0.11:4646
   ./bootstrap.sh
   ```

5. **Point OSimFlow at the cluster:**

   ```bash
   export NOMAD_ADDR=http://10.0.0.11:4646
   export NOMAD_TOKEN=<worker token from bootstrap output>
   ```

## Teardown

```bash
sudo systemctl stop nomad   # on each server and client node
```

## Migration from the removed `hind` (Docker Compose) setup

If you previously brought the cluster up with `docker compose up -d` against
the old `docker-compose.yml`:

1. `docker compose down -v` on the old stack to release the data volumes.
2. Provision 3 server hosts + N client hosts (VMs or bare metal).
3. Install the `nomad` binary and the Docker engine (for the task driver)
   on each host.
4. Follow **Bring-up** above. The ACL policies and `osimflow_worker.hcl`
   job spec are unchanged, so existing worker tokens remain valid until
   they expire.

The workload contract is identical — `NomadExecutor` still dispatches
unprivileged Docker tasks — so no OSimFlow CLI or campaign-config changes
are required.
