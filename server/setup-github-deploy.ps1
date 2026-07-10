<#
.SYNOPSIS
    One-time GCP provisioning for GitHub Actions deploys of duet-bridge.

.DESCRIPTION
    Creates a Workload Identity Federation pool + OIDC provider trusted for
    this GitHub repository, a github-deployer service account with the roles
    needed for `gcloud run deploy --source`, and (if the gh CLI is available)
    sets the two GitHub repo variables the workflow reads:
        GCP_WIF_PROVIDER  - full resource name of the WIF provider
        GCP_DEPLOYER_SA   - deployer service-account email

    Idempotent: safe to re-run; existing resources are reused.

.PARAMETER Project
    GCP project id. Defaults to asc-router — the project the live duet-bridge
    service actually runs in, which is NOT the ambient gcloud config default
    on this machine. Deliberately not read from gcloud config for that reason.

.PARAMETER Repo
    GitHub repo (owner/name) allowed to impersonate the deployer SA.
#>
[CmdletBinding()]
param(
    [string]$Project    = 'asc-router',
    [string]$Repo       = 'acor8826/duet-build',
    [string]$PoolId     = 'github-pool',
    [string]$ProviderId = 'github-provider',
    [string]$SaName     = 'github-deployer'
)

$ErrorActionPreference = 'Stop'

# Same stderr-tolerant gcloud wrapper as deploy.ps1: PowerShell 5.1 turns
# gcloud's routine stderr progress into terminating NativeCommandErrors under
# ErrorActionPreference=Stop, so merge streams and gate on $LASTEXITCODE.
function Invoke-Gcloud {
    [CmdletBinding()]
    param([Parameter(Mandatory, ValueFromRemainingArguments)][string[]]$GcloudArgs)
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    & gcloud @GcloudArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { [Console]::Error.WriteLine($_.ToString()) }
        else { $_ }
    }
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($GcloudArgs -join ' ') failed with exit $LASTEXITCODE" }
}

function Test-GcloudResource {
    param([Parameter(Mandatory)][string[]]$GcloudArgs)
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    & gcloud @GcloudArgs 2>&1 | Out-Null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = 'Stop'
    return $ok
}

if (-not $Project) { throw 'No GCP project id. Pass -Project (the live service runs in asc-router).' }

Write-Output "project=$Project repo=$Repo pool=$PoolId provider=$ProviderId sa=$SaName"

Invoke-Gcloud services enable iamcredentials.googleapis.com sts.googleapis.com --project $Project

$projNum = (& gcloud projects describe $Project --format 'value(projectNumber)' 2>$null)
if (-not $projNum) { throw "Could not resolve project number for $Project (is gcloud authenticated?)." }

$saEmail = "$SaName@$Project.iam.gserviceaccount.com"

# --- Workload Identity Federation pool + provider -------------------------
if (-not (Test-GcloudResource @('iam', 'workload-identity-pools', 'describe', $PoolId, '--location=global', "--project=$Project"))) {
    Write-Output "Creating WIF pool $PoolId..."
    Invoke-Gcloud iam workload-identity-pools create $PoolId --location=global --display-name 'GitHub Actions' --project $Project
} else {
    Write-Output "WIF pool $PoolId already exists."
}

if (-not (Test-GcloudResource @('iam', 'workload-identity-pools', 'providers', 'describe', $ProviderId, '--location=global', "--workload-identity-pool=$PoolId", "--project=$Project"))) {
    Write-Output "Creating WIF provider $ProviderId (restricted to $Repo)..."
    Invoke-Gcloud iam workload-identity-pools providers create-oidc $ProviderId `
        --location=global `
        --workload-identity-pool=$PoolId `
        --issuer-uri 'https://token.actions.githubusercontent.com' `
        --attribute-mapping 'google.subject=assertion.sub,attribute.repository=assertion.repository' `
        --attribute-condition "assertion.repository=='$Repo'" `
        --project $Project
} else {
    Write-Output "WIF provider $ProviderId already exists."
}

# --- Deployer service account + roles --------------------------------------
if (-not (Test-GcloudResource @('iam', 'service-accounts', 'describe', $saEmail, "--project=$Project"))) {
    Write-Output "Creating service account $saEmail..."
    Invoke-Gcloud iam service-accounts create $SaName --display-name 'GitHub Actions deployer (duet-bridge)' --project $Project
} else {
    Write-Output "Service account $saEmail already exists."
}

# Roles required for `gcloud run deploy --source`:
#   run.admin                       deploy/update the Cloud Run service
#   cloudbuild.builds.editor        --source deploys submit a Cloud Build
#   storage.admin                   upload source tarball to the build bucket
#   secretmanager.viewer            the workflow probes for duet-anthropic-key
#   serviceusage.serviceUsageConsumer  quota/project API usage during build
foreach ($role in @(
        'roles/run.admin',
        'roles/cloudbuild.builds.editor',
        'roles/storage.admin',
        'roles/secretmanager.viewer',
        'roles/serviceusage.serviceUsageConsumer')) {
    Write-Output "Granting $role..."
    Invoke-Gcloud projects add-iam-policy-binding $Project --member "serviceAccount:$saEmail" --role $role --condition=None | Out-Null
}

# Cloud Run runs the service as the default compute SA; the deployer must be
# allowed to "act as" it.
$runtimeSa = "$projNum-compute@developer.gserviceaccount.com"
Write-Output "Granting iam.serviceAccountUser on $runtimeSa..."
Invoke-Gcloud iam service-accounts add-iam-policy-binding $runtimeSa `
    --member "serviceAccount:$saEmail" `
    --role roles/iam.serviceAccountUser `
    --project $Project | Out-Null

# Allow workflows from this repo (any branch/event; the workflow itself gates
# deploys to master) to impersonate the deployer SA via the WIF provider.
$principal = "principalSet://iam.googleapis.com/projects/$projNum/locations/global/workloadIdentityPools/$PoolId/attribute.repository/$Repo"
Write-Output 'Binding WIF principal to the deployer SA...'
Invoke-Gcloud iam service-accounts add-iam-policy-binding $saEmail `
    --member $principal `
    --role roles/iam.workloadIdentityUser `
    --project $Project | Out-Null

$providerResource = "projects/$projNum/locations/global/workloadIdentityPools/$PoolId/providers/$ProviderId"

# --- Publish the two values as GitHub repo variables ------------------------
$gh = Get-Command gh -ErrorAction SilentlyContinue
if ($gh) {
    Write-Output 'Setting GitHub repo variables via gh...'
    & gh variable set GCP_WIF_PROVIDER --repo $Repo --body $providerResource
    & gh variable set GCP_DEPLOYER_SA --repo $Repo --body $saEmail
} else {
    Write-Output 'gh CLI not found - set these repo variables manually (GitHub repo Settings > Secrets and variables > Actions > Variables):'
}

Write-Output ''
Write-Output '=== GitHub Actions deploy is provisioned ==='
Write-Output "GCP_WIF_PROVIDER = $providerResource"
Write-Output "GCP_DEPLOYER_SA  = $saEmail"
Write-Output 'Pushes to master touching server/** will now deploy via .github/workflows/deploy-duet-bridge.yml.'
