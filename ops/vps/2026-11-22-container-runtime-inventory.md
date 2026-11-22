# VPS container runtime inventory — 2026-11-22

Status: **read-only host inspection; no migration or cutover performed**.
Source: commands and output captured interactively on the production Ubuntu 26.04 VPS (ps, docker ps -a, podman ps -a, systemctl, loginctl, and rootless Podman checks). This is a point-in-time inventory, **not** a complete security audit or health check.

## Companions: already rootless Podman

The host-level 'podman ps -a' run as root returned no containers. That is expected because Podman storage is per-user; it **does not** imply the Companions service is absent.

Process ownership and rootless Podman paths establish two distinct running stacks:

| Stack | OS account | Observed containers |
| --- | --- | --- |
| Companions production | 'svc-companions' (UID 1004) | 'companions-backend', 'companions-mcp' |
| Companions canary | 'svc-companions-canary' (UID 1003) | 'companions-canary-backend', 'companions-canary-mcp' |

Each stack has its own user-owned Podman container storage and runtime directory. Their conmon processes and MCP application processes were observed running. A direct authenticated MCP health/integration test was **not** part of this inspection.

**Decision:** Do not migrate these working rootless stacks into the new 'luna' account or replace them with root-managed containers. Preserve the existing service-account isolation and production/canary separation.

## Root-managed Docker workloads

The root-owned Docker daemon was running these containers as of the inspection:

| Workload | Container(s) | Observed state |
| --- | --- | --- |
| Life MCP | 'profile-os-life-mcp-1' | Running; Docker reported healthy |
| Vesper MCP | 'vesper-mcp-vesper-mcp-1' | Running; Docker reported healthy |
| Companions Forum | 'companions-forum-forum-1', 'companions-forum-gateway-1', 'companions-forum-database-1' (MariaDB) | Running; Forum and DB reported healthy |
| Jarvis | 'jarvis-jarvis-1' | Running |
| ChatGPT export browser | 'chatgpt-export-browser' | Running |

The old Docker 'profile-os-mcp-1' and 'profile-os-backend-1' containers existed but were **exited** at the time. Their presence is not evidence that they serve the production Companions endpoint. Do not remove them or their volumes without identifying their rollback/data dependencies.

## Rootless account bootstrap

A separate 'luna' account was provisioned (UID 1005), password locked, without sudo membership. Subordinate UID/GID allocation starts at 427680 with 65536 IDs. User lingering is enabled and its systemd user manager is active. Podman checks returned 'Rootless=true' and 'CgroupManager=systemd' when invoked with Luna's user D-Bus environment. This account is **not** the owner of the production/canary Companions containers.

## Capacity snapshot

At inspection: approximately 3.7 GiB RAM total, 1.4 GiB available, no swap; root filesystem 38 GiB total, approximately 10 GiB available (73% used). Container rebuilds and parallel cutovers should account for this limited headroom.

## Operations cautions

- Do **not** apply historical Docker Compose Companions deployment commands without first identifying which runtime currently serves production. The older DEPLOY.md instructions describe Docker-based releases and may no longer match the active runtime.
- Verify live unit definitions, images, mounts, volumes, persistent data, network routes, health checks, backup/restore and rollback before changing any deployment.
- Scope any future move from root-managed Docker to rootless Podman to an individual service. Do not cut over multiple services together or run two database writers on the same volume.
- Changes to GitHub repository ownership are separate from runtime migration; neither implies a VPS service change.
