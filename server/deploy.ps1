<#
.SYNOPSIS
    Deploy the duet-bridge FastMCP server to Cloud Run in australia-southeast1.

.DESCRIPTION
    Prerequisites the user must have completed:
      1. `gcloud auth login` already done.
      2. A GCP project with billing enabled. (Script will prompt if `gcloud
         config get project` is unset.)
      3. OPENAI_API_KEY stored in Secret Manager as `duet-openai-key`. The
         script will create the secret from a prompt if it is absent.

    The script enables required APIs (run.googleapis.com,
    secretmanager.googleapis.com, cloudbuild.googleapis.com) before deploy.

.PARAMETER Project
    GCP project id. If omitted, uses `gcloud config get project`.

.PARAMETER Region
    Defaults to australia-southeast1.

.PARAMETER ServiceName
    Defaults to duet-bridge.

.PARAMETER PartnerModel
    Defaults to gpt-5.5.
#>
[CmdletBinding()]
param(
    [string]$Project,
    [string]$Region       = 'australia-southeast1',
    [string]$ServiceName  = 'duet-bridge',
    [string]$PartnerModel = 'gpt-5.5'
)

$ErrorActionPreference = 'Stop'

function Require-Gcloud {
    $g = Get-Command gcloud -ErrorAction SilentlyContinue
    if (-not $g) {
        throw "gcloud CLI not found. Install Google Cloud SDK and run 'gcloud auth login' first."
    }
}

Require-Gcloud

if (-not $Project) {
    $Project = (& gcloud config get-value project 2>$null)
    if (-not $Project -or $Project -eq '(unset)') {
        $Project = Read-Host -Prompt 'Enter GCP project id'
    }
}
if (-not $Project) { throw 'No GCP project id provided.' }

Write-Output "project=$Project region=$Region service=$ServiceName partner_model=$PartnerModel"

Write-Output 'Enabling required APIs...'
& gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com --project $Project

# Ensure Secret Manager secret exists.
$secretName = 'duet-openai-key'
$existing = & gcloud secrets describe $secretName --project $Project 2>$null
if ($LASTEXITCODE -ne 0) {
    $apiKey = Read-Host -Prompt 'Enter OPENAI_API_KEY (will be stored in Secret Manager)' -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKey)
    $apiKeyPlain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Write-Output 'Creating secret duet-openai-key...'
    $apiKeyPlain | & gcloud secrets create $secretName --data-file=- --replication-policy=automatic --project $Project
} else {
    Write-Output "Secret $secretName already exists; using latest version."
}

# Ensure bearer-token secret exists (gates the public Cloud Run endpoint).
$bearerSecretName = 'duet-mcp-bearer'
$bearerExisting = & gcloud secrets describe $bearerSecretName --project $Project 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Output "Generating new bearer token and storing in Secret Manager as $bearerSecretName..."
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $bearerPlain = [Convert]::ToBase64String($bytes)
    $bearerPlain | & gcloud secrets create $bearerSecretName --data-file=- --replication-policy=automatic --project $Project
    Write-Output ''
    Write-Output '=== COPY THIS BEARER TOKEN (you will paste it into claude.ai) ==='
    Write-Output $bearerPlain
    Write-Output '================================================================='
    Write-Output ''
} else {
    Write-Output "Secret $bearerSecretName already exists; using latest version. Retrieve with:"
    Write-Output "  gcloud secrets versions access latest --secret=$bearerSecretName --project=$Project"
}

$srcDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Output 'Deploying to Cloud Run...'
& gcloud run deploy $ServiceName `
    --source $srcDir `
    --region $Region `
    --project $Project `
    --allow-unauthenticated `
    --set-env-vars "OPENAI_PARTNER_MODEL=$PartnerModel,DUET_TRANSPORT=http,DUET_STATE_DIR=/tmp/duet-state,DUET_ITERATION_CAP=8,DUET_CONFIDENCE_THRESHOLD=95" `
    --set-secrets "OPENAI_API_KEY=$($secretName):latest,DUET_MCP_BEARER=$($bearerSecretName):latest" `
    --memory 512Mi `
    --cpu 1 `
    --concurrency 4 `
    --max-instances 3 `
    --timeout 900

if ($LASTEXITCODE -ne 0) {
    throw "gcloud run deploy failed with exit $LASTEXITCODE"
}

Write-Output 'Fetching service URL...'
$url = & gcloud run services describe $ServiceName --region $Region --project $Project --format 'value(status.url)'
Write-Output "DUET_BRIDGE_URL=$url"
Write-Output ''
Write-Output 'Done. Set DUET_BRIDGE_URL in your local .env to point Claude Code at this service.'
