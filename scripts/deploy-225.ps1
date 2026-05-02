param(
    [string]$HostAlias = "mfin-225",
    [string]$RemoteDir = "/opt/homehub",
    [string]$ArtifactName = "homehub-deploy.tgz"
)

$ErrorActionPreference = "Stop"

Write-Host "[deploy] Packaging workspace..."
if (Test-Path $ArtifactName) {
    Remove-Item $ArtifactName -Force
}

tar -czf $ArtifactName `
  --exclude=$ArtifactName `
  --exclude=.git `
  --exclude=.venv `
  --exclude=node_modules `
  --exclude=data `
  --exclude=uploads `
  --exclude=media `
  --exclude=pdfs `
  --exclude=temp `
  --exclude=.env `
  --exclude=config.yml `
  .

Write-Host "[deploy] Uploading artifact to $HostAlias..."
scp $ArtifactName "$HostAlias`:/tmp/$ArtifactName"

Write-Host "[deploy] Applying release on remote host..."
ssh $HostAlias "set -e; mkdir -p $RemoteDir; tar -xzf /tmp/$ArtifactName -C $RemoteDir; rm -f /tmp/$ArtifactName; cd $RemoteDir; mkdir -p data uploads media pdfs; [ -f .env ] || cp .env.example .env; [ -f config.yml ] || cp config-example.yml config.yml; docker compose -f docker-compose.prod.yml --env-file .env up -d --build; docker compose -f docker-compose.prod.yml --env-file .env ps"

Write-Host "[deploy] Done. If first deploy, edit $RemoteDir/.env and $RemoteDir/config.yml on host, then re-run this script."