# Companions rootless Podman deployment runbook

**Captured:** 2026-11-22. **Status:** operational guide based on a read-only VPS process inventory, repository documentation, and a verified rootless Podman account test. This is **not** proof of all production deployment details. Do not treat commands here as authorization for a live cutover.

## Where Companions is deployed

The VPS is an Ubuntu 26.04 host. The active Companions containers are **not** in root's Podman storage; they are already rootless under distinct service identities:

| Environment | Linux owner | UID | Observed container names | Podman data path observed |
| --- | --- | --- | --- | --- |
| Production | svc-companions | 1004 | companions-backend, companions-mcp | /home/svc-companions/.local/share/containers/storage |
| Canary | svc-companions-canary | 1003 | companions-canary-backend, companions-canary-mcp | /home/svc-companions-canary/.local/share/containers/storage |

The corresponding runtime directories were observed under /run/user/1004 and /run/user/1003. Running conmon processes and MCP application processes confirmed these identities and container names. The exact user systemd/Quadlet unit names, current image digests, mounted database locations, proxy mappings, and effective deployment scripts **have not yet been verified in this inventory**. Discover them on-host before issuing any restart command.

**Do not use root's 'podman ps' to assess these workloads:** it reported no containers because root has a separate container store. Do not migrate Companions into 'luna', root Docker, or any other account. Production and canary must remain isolated from one another.

A separate rootless 'luna' account (UID 1005) was provisioned for testing/non-production work. It has no sudo membership, password login locked, a dedicated subordinate UID/GID range (427680:65536), lingering enabled, and a working systemd user manager. Its Podman check returned 'true systemd'. **Luna is not the production service owner and must not receive Companions credentials or writable production data.**

The host also retains root-managed Docker workloads for Life MCP, Vesper MCP, the Forum/MariaDB, Jarvis, and the ChatGPT export browser. These are separate migrations. Two older Companions Docker containers appeared *exited*; their existence does not make them the active deployment or safe deletion targets.

## Where code and configuration belong

- **Authoritative source:** GitHub repository 'andresmejiaro/companionSystem' on the date of this note. Transfer to the DataCodeMath organization was requested, but not yet verified; once transferred, use 'datacodemath/companionSystem' as the canonical owner.
- **Operational record:** this runbook in 'ops/vps/'. See '2026-11-22-container-runtime-inventory.md' for supporting observations.
- **Version to deploy:** an explicit reviewed Git commit SHA and ideally an immutable image digest. Record both in the release receipt. Never assume that a local worktree, a floating image tag, or a Docker Compose override still corresponds to the active rootless deployment.
- **Runtime configuration/secrets:** live exclusively in the service owners' protected server configuration/secret stores. Do not copy secrets into GitHub, the Luna account, Codex chats, logs, or receipts.
- **State:** database/files must stay on their currently verified persistent mount or volume. The exact production and canary mount paths were not established by the recorded inspection; find and back them up before updating images.

**Warning:** the repository's historic 'DEPLOY.md' currently starts with a Docker Compose procedure under /opt/profile-os for a 2026-11-21 release. That historical release and its rollback artifacts may still be relevant, but these commands **must not be used blindly** on the current rootless Companions stacks. Resolve the actual rootless units and data mounts first; reconcile the old runbook afterward.

## Safe inventory before any deployment

As the host administrator, inspect accounts and per-user services without stopping anything. The following commands are **read-only** and avoid printing environment variables or secrets:

~~~bash
cd /tmp
for svc in svc-companions-canary svc-companions; do
  uid=$(id -u "$svc")
  home=$(getent passwd "$svc" | cut -d: -f6)
  echo "=== $svc / UID $uid ==="
  loginctl show-user "$svc" -p Linger -p State -p RuntimePath
  runuser -u "$svc" -- env HOME="$home" \
    XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    podman ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
  runuser -u "$svc" -- env HOME="$home" \
    XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user list-unit-files --no-pager
  runuser -u "$svc" -- env HOME="$home" \
    XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user list-units --all --type=service --no-pager
done
~~~

For each of the four known containers, inspect selected non-secret properties (image ID, mounts, ports, labels, pod membership), using the *correct owner account* and Podman command. **Do not dump all environment variables, authentication headers, 'podman inspect' output wholesale, or private config contents to a public report.** Determine which user units/Quadlets start the containers by matching actual systemd units and container names. On the host, separately identify the Caddy target and any socket/port routing, without altering it.

If the effective unit files, deployment working directory, image digest, state mounts, and backup/rollback procedure cannot be established, **stop here** and return an inventory/blocker report.

## How to deploy an update safely

1. **Prepare from GitHub.** Select a reviewed commit, ensure changes/tests are documented, identify the immutable image digest and the previous known-good image. Prefer a GitHub-traceable build/release artifact. Do not patch application code directly on the VPS.
2. **Inspect and protect state.** Record live unit names, image pins, the actual persistent database/files mounts, service-to-service dependencies and network endpoints. Verify a recoverable backup of production state. Do not mount one writable SQLite database into two independently running instances.
3. **Deploy to canary first.** Under **svc-companions-canary**, update only its established service definition/image reference using the existing deployment mechanism. Any 'systemctl --user daemon-reload' or 'systemctl --user restart <VERIFIED_UNIT>' must be run in that service owner's working user systemd environment; '<VERIFIED_UNIT>' is a placeholder, **not an observed unit name**. Do not touch production yet.
4. **Verify canary.** Confirm the expected image digest, container and user-unit health, network route, and correct attached state. Exercise its existing health endpoint and representative authenticated MCP read paths. Perform writes only against disposable/test data with explicit authorization. Confirm production remains healthy in parallel.
5. **Deploy production deliberately.** After canary acceptance and a verified restore/rollback plan, use the **svc-companions** account and its *verified* unit names to change only the approved production image/config. Preserve production's data mount and endpoint. Avoid concurrent database writers during any cutover.
6. **Verify the live public route.** Compare the approved image/commit with what is running; check the production health endpoint and authenticated MCP functionality from an actual client (including ChatGPT if relevant), not just a local container process. Record results, timestamp and any issues.
7. **Rollback on failure.** Restore the known-good image/unit configuration and restart only the affected unit via its original service owner. Restore database state only using the verified, migration-aware recovery plan; reverting application code alone may not revert schema changes. Do not delete old images/volumes or modify another service to hide the failure.

No exact restart, build, or backup command is asserted here because the actual per-user units, bind mounts and release artifacts were **not** established by the captured inspection. The first safe deployment-specific task is to verify and document those values from the host.

## Operating boundaries / failure prevention

- **Environment owners are not interchangeable:** canary = svc-companions-canary; production = svc-companions; luna = separate non-production account; root = host bootstrap/admin only.
- Rootless Podman should be managed with the owner's user service manager. Invoking 'runuser' without an appropriate user environment can cause Podman to fall back from systemd cgroups to cgroupfs; the Luna checks established the correct HOME, XDG_RUNTIME_DIR and D-Bus environment.
- Keep production available while preparing and testing canary. Avoid changing Caddy, OAuth, firewall, ports or databases without a concrete need and rollback.
- A verified running process is not equivalent to a validated health check. A local healthy response is not equivalent to authenticated end-to-end MCP success.
- Host capacity at this snapshot: 3.7 GiB RAM total, ~1.4 GiB available, no swap; 38 GiB root filesystem with ~10 GiB free. Avoid simultaneous large builds/images.
- Preserve an auditable release receipt with source SHA, image digest, owner, unit name, mounts (paths only, no secrets), preflight/backup proof, canary result, production result and rollback reference.
- Keep production deployment/migration separate from Yoga's AI task-worker development and its queue routing changes.

## What is still unknown (mandatory follow-up evidence)

- Exact user systemd or Quadlet unit names for the two production and two canary containers.
- Current images/digests and how rootless deployments are built or pulled.
- Actual persistent volume/bind paths, SQLite writer isolation, backup and restore tests.
- Active Caddy/reverse-proxy targets and canary-only endpoint.
- Last successfully tested production rollback instructions.

Until these are verified, use this as the **deployment boundary and verification guide**, not a copy/paste production cutover script.

Related: 'ops/vps/2026-11-22-container-runtime-inventory.md', repository root 'DEPLOY.md' (historical Docker instructions), 'MCP_CONNECTOR.md'.
