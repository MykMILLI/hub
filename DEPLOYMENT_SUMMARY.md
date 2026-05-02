# HomeHub Deployment Summary

This file defines the recommended deployment approach for HomeHub so it can run continuously in production while still allowing fast ongoing updates.

## Deployment model

- Develop and test locally.
- Deploy to a dedicated host using Docker Compose production config.
- Keep runtime secrets outside source control in a host-only .env file.
- Persist user and app data in mounted host directories.

## Production files used

- docker-compose.prod.yml
  - Runs the production container with restart policy.
  - Uses .env for secrets and runtime configuration.
  - Includes healthcheck and log rotation.
  - Mounts persistent data folders.
  - Mounts config.yml writable so Settings page changes can be saved.

- .env (on host, not committed)
  - Holds SECRET_KEY, Plaid credentials, SMTP credentials, and runtime port.

- scripts/deploy-225.ps1
  - Packages the project.
  - Uploads to host.
  - Applies release on host.
  - Starts/updates service with docker compose.

## Host currently configured

- Host: 192.168.1.225
- SSH alias: homehub-225
- Service path: /opt/homehub
- Exposed app port: 5001
- App URL: http://192.168.1.225:5001/

## Port selection strategy

- Check host ports before deployment to avoid conflicts.
- Current used ports observed on host include: 22, 8000, 8080, 9443, 34400, 35400.
- HomeHub is set to use 5001 to stay clear of existing services.

## Ongoing update workflow

1. Make and test changes locally.
2. Run deployment script to publish latest build to host.
3. Validate service status and app reachability.

PowerShell:

./scripts/deploy-225.ps1 -HostAlias homehub-225

## Security baseline

- No hardcoded production secrets in compose files.
- Keep .env file only on deployment host.
- Restrict .env file permissions to owner-only read/write.
- Use restart unless-stopped so app returns after host reboot.
- Use no-new-privileges container security option.
- Keep config.yml mounted persistently (writable) to allow admin settings updates in-app.
- Keep data directories persistent:
  - /opt/homehub/data
  - /opt/homehub/uploads
  - /opt/homehub/media
  - /opt/homehub/pdfs

## Recommended hardening next

- Put HomeHub behind HTTPS reverse proxy (Caddy, Traefik, or Nginx).
- Add firewall rules to permit only required inbound ports.
- Rotate any secrets that were previously hardcoded in older configs.
- Schedule automated backups of data folders plus config.yml and .env.
- Add deployment health checks/alerts for uptime monitoring.

## Rollback approach

- Keep previous deployment tarball or git snapshot.
- If a release fails:
  - restore previous source snapshot in /opt/homehub
  - restart with docker compose using same .env
- Always verify container health and homepage response after rollback.

## Quick verification commands

- Service status:
  - ssh homehub-225 "docker compose -f /opt/homehub/docker-compose.prod.yml --env-file /opt/homehub/.env ps"

- App response:
  - curl -I http://192.168.1.225:5001/
