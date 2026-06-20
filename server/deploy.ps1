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

.PARAMETER DriveConnectorId
    OpenAI Google Drive connector id (e.g. connector_googledrive). When supplied,
    the deploy enables the Responses-API path (DUET_USE_RESPONSES_API=1) so the GPT
    side reads the case folders from Drive itself. Requires the OAuth token stored in
    Secret Manager as `duet-drive-auth`. Omit to deploy without live Drive (default).

.PARAMETER DriveFolderIds
    Comma-separated Drive folder ids for the case folders, surfaced to GPT as a hint only.
    This is NOT a security boundary: drive.readonly is all-or-nothing, so real least-
    privilege requires a dedicated Google account / restricted shared drive that can only
    see those folders.
#>
[CmdletBinding()]
param(
    [string]$Project,
    [string]$Region          = 'australia-southeast1',
    [string]$ServiceName     = 'duet-bridge',
    [string]$PartnerModel    = 'gpt-5.5',
    [string]$DriveConnectorId = '',
    [string]$DriveFolderIds   = ''
)

$ErrorActionPreference = 'Stop'

function Require-Gcloud {
    $g = Get-Command gcloud -ErrorAction SilentlyContinue
    if (-not $g) {
        throw "gcloud CLI not found. Install Google Cloud SDK and run 'gcloud auth login' first."
    }
}

# gcloud writes routine progress to stderr. Under this script's
# $ErrorActionPreference='Stop' (PowerShell 5.1) those writes surface as
# terminating NativeCommandError exceptions and abort an otherwise-successful
# deploy. These helpers merge stderr back into the output stream (so it stays
# visible) and gate on the real success signal -- $LASTEXITCODE -- instead.
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

function Invoke-GcloudPiped {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$InputText,
        [Parameter(Mandatory, ValueFromRemainingArguments)][string[]]$GcloudArgs
    )
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    $InputText | & gcloud @GcloudArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { [Console]::Error.WriteLine($_.ToString()) }
        else { $_ }
    }
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($GcloudArgs -join ' ') failed with exit $LASTEXITCODE" }
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
Invoke-Gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com --project $Project

# Ensure Secret Manager secret exists.
$secretName = 'duet-openai-key'
$existing = & gcloud secrets describe $secretName --project $Project 2>$null
if ($LASTEXITCODE -ne 0) {
    $apiKey = Read-Host -Prompt 'Enter OPENAI_API_KEY (will be stored in Secret Manager)' -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKey)
    $apiKeyPlain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Write-Output 'Creating secret duet-openai-key...'
    Invoke-GcloudPiped -InputText $apiKeyPlain secrets create $secretName --data-file=- --replication-policy=automatic --project $Project
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
    Invoke-GcloudPiped -InputText $bearerPlain secrets create $bearerSecretName --data-file=- --replication-policy=automatic --project $Project
    Write-Output ''
    Write-Output '=== COPY THIS BEARER TOKEN (you will paste it into claude.ai) ==='
    Write-Output $bearerPlain
    Write-Output '================================================================='
    Write-Output ''
} else {
    Write-Output "Secret $bearerSecretName already exists; using latest version. Retrieve with:"
    Write-Output "  gcloud secrets versions access latest --secret=$bearerSecretName --project=$Project"
}

# Anthropic key (Opus role for the server-side duet_run tool). Optional: if the
# secret is absent we deploy without it and duet_run reports itself unavailable
# until the secret is added and the service is redeployed. Create it with:
#   echo -n "<your-anthropic-key>" | gcloud secrets create duet-anthropic-key \
#       --data-file=- --replication-policy=automatic --project=<project>
$anthropicSecretName = 'duet-anthropic-key'
# Probe for the secret without letting gcloud's NOT_FOUND stderr abort the script
# under $ErrorActionPreference='Stop' (PowerShell 5.1 wraps native stderr as a
# terminating NativeCommandError). Continue + swallow, gate on $LASTEXITCODE.
$anthropicExists = $false
try {
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    & gcloud secrets describe $anthropicSecretName --project $Project 2>&1 | Out-Null
    $anthropicExists = ($LASTEXITCODE -eq 0)
} catch {
    $anthropicExists = $false
} finally {
    $ErrorActionPreference = 'Stop'
}
$secretMap = "OPENAI_API_KEY=$($secretName):latest,DUET_MCP_BEARER=$($bearerSecretName):latest"
if ($anthropicExists) {
    Write-Output "Secret $anthropicSecretName found; enabling server-side duet_run (Opus role)."
    $secretMap += ",ANTHROPIC_API_KEY=$($anthropicSecretName):latest"
    # Ensure the Cloud Run runtime service account can read the secret (idempotent).
    $projNum = (& gcloud projects describe $Project --format 'value(projectNumber)' 2>$null)
    if ($projNum) {
        $runtimeSa = "$projNum-compute@developer.gserviceaccount.com"
        Write-Output "Ensuring $runtimeSa can access $anthropicSecretName..."
        Invoke-Gcloud secrets add-iam-policy-binding $anthropicSecretName `
            --member "serviceAccount:$runtimeSa" `
            --role roles/secretmanager.secretAccessor `
            --project $Project | Out-Null
    }
} else {
    Write-Output "Secret $anthropicSecretName NOT found; deploying without it (duet_run will report unavailable)."
    Write-Output "  Add it later, then re-run this script:"
    Write-Output "  echo -n '<key>' | gcloud secrets create $anthropicSecretName --data-file=- --replication-policy=automatic --project=$Project"
}

# Base environment. Live Google Drive (the Responses-API path) is OFF unless a Drive
# connector id is passed; the base deploy is byte-for-byte the prior behaviour.
$envVars = "OPENAI_PARTNER_MODEL=$PartnerModel,DUET_TRANSPORT=http,DUET_STATE_DIR=/tmp/duet-state,DUET_ITERATION_CAP=8,DUET_CONFIDENCE_THRESHOLD=95,DUET_OPUS_MODEL=claude-opus-4-8,DUET_OPENAI_TIMEOUT=150,DUET_MAX_OUTPUT_TOKENS=4000,DUET_OUTPUT_TOKEN_PARAM=max_completion_tokens,DUET_MAX_TOTAL_DOC_CHARS=120000"

if ($DriveConnectorId) {
    $driveSecretName = 'duet-drive-auth'
    $driveExists = $false
    try {
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 0
        & gcloud secrets describe $driveSecretName --project $Project 2>&1 | Out-Null
        $driveExists = ($LASTEXITCODE -eq 0)
    } catch {
        $driveExists = $false
    } finally {
        $ErrorActionPreference = 'Stop'
    }
    if (-not $driveExists) {
        throw ("DriveConnectorId was supplied but secret $driveSecretName is missing. Create it first:`n" +
            "  echo -n '<oauth-access-token>' | gcloud secrets create $driveSecretName --data-file=- --replication-policy=automatic --project=$Project")
    }
    Write-Output "Drive connector '$DriveConnectorId' configured; enabling live Drive (Responses API)."
    $envVars += ",DUET_USE_RESPONSES_API=1,DUET_DRIVE_CONNECTOR_ID=$DriveConnectorId"
    if ($DriveFolderIds) { $envVars += ",DUET_DRIVE_FOLDER_IDS=$DriveFolderIds" }
    $secretMap += ",DUET_DRIVE_AUTH=$($driveSecretName):latest"
    $projNum = (& gcloud projects describe $Project --format 'value(projectNumber)' 2>$null)
    if ($projNum) {
        $runtimeSa = "$projNum-compute@developer.gserviceaccount.com"
        Write-Output "Ensuring $runtimeSa can access $driveSecretName..."
        Invoke-Gcloud secrets add-iam-policy-binding $driveSecretName `
            --member "serviceAccount:$runtimeSa" `
            --role roles/secretmanager.secretAccessor `
            --project $Project | Out-Null
    }
} else {
    Write-Output "No DriveConnectorId; deploying without live Drive (GPT falls back to request_document)."
}

$srcDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Output 'Deploying to Cloud Run...'
# Invoke-Gcloud already throws on a non-zero exit, so no separate check is needed.
Invoke-Gcloud run deploy $ServiceName `
    --source $srcDir `
    --region $Region `
    --project $Project `
    --allow-unauthenticated `
    --set-env-vars $envVars `
    --set-secrets $secretMap `
    --memory 512Mi `
    --cpu 1 `
    --concurrency 4 `
    --max-instances 3 `
    --timeout 900

Write-Output 'Fetching service URL...'
$url = Invoke-Gcloud run services describe $ServiceName --region $Region --project $Project --format 'value(status.url)'
Write-Output "DUET_BRIDGE_URL=$url"
Write-Output ''
Write-Output 'Done. Set DUET_BRIDGE_URL in your local .env to point Claude Code at this service.'
