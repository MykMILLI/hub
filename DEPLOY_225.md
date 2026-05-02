# HomeHub Deployment (192.168.1.225)

This setup deploys HomeHub to host `mfin-225` with Docker Compose using an `.env` file for secrets.

## 1) Verify SSH alias (local machine)

Your local SSH config should include:

```sshconfig
Host mfin-225
  HostName 192.168.1.225
  User root
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
```

## 2) First-time setup on host

Run once from repo root (PowerShell):

```powershell
./scripts/deploy-225.ps1
```

Then SSH into host and set real secrets:

```bash
ssh mfin-225
cd /opt/homehub
nano .env
nano config.yml
```

## 3) Deploy updates any time

After code changes:

```powershell
./scripts/deploy-225.ps1
```

## 4) Port usage / chosen app port

Current open ports on `192.168.1.225`: `22`, `8000`, `8080`, `9443`, `34400`, `35400`.

Default HomeHub production port is set to `5001` in `.env.example` to avoid conflicts.

## 5) Security baseline included

- No hardcoded secrets in compose files.
- `.env` file required on host for sensitive values.
- `config.yml` mounted writable in production so Settings updates persist.
- `restart: unless-stopped` for long-running service.
- `no-new-privileges` security option.
- Healthcheck enabled.
- Log rotation enabled via Docker json-file options.

## 6) Power-loss resilience (auto-recovery)

- HomeHub now exposes a lightweight health endpoint at `/healthz` for fast container health checks.
- Deployment includes an `autoheal` container that watches for unhealthy containers and restarts them automatically.
- This prevents stuck-unhealthy states after host power restoration.

## 7) Recommended next hardening

- Put HomeHub behind reverse proxy with TLS (Caddy/Traefik/Nginx Proxy Manager).
- Restrict host firewall to required inbound ports only.
- Rotate any credentials previously committed in plaintext.
- Schedule periodic backups of `/opt/homehub/data`, `/opt/homehub/uploads`, `/opt/homehub/media`, `/opt/homehub/pdfs`, and `/opt/homehub/.env`.