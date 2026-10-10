<#
.SYNOPSIS
Check for, and optionally apply, a mnemosyne-hermes update (native Windows).

.DESCRIPTION
  scripts\update.ps1                  tell me if an update exists (default; changes nothing)
  scripts\update.ps1 -Quiet           with -Check: print nothing when up to date (for scheduled jobs)
  scripts\update.ps1 -Apply           fast-forward the checkout, re-run install.ps1 -Yes, verify

-Venv, -Python and -HermesHome are forwarded to install.ps1 when given.

Exit status: 0 up to date or applied and verified; 10 -Check only, update
available; 1 failure (an -Apply that fails after the fast-forward is rolled
back); 2 usage error.

-Apply runs the installer from the fetched commits, so only schedule it for a
remote you trust. It refuses a dirty tree, never merges (fast-forward only),
never touches the memory database, and restores the previous commit and install
when the install or `hermes mnemosyne doctor --no-fix` fails. Restart the
Hermes gateway after an applied update.
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Apply,
    [switch]$Quiet,
    [string]$Remote = 'origin',
    [string]$Branch = 'main',
    [string]$Venv,
    [string]$Python,
    [string]$HermesHome
)

$ErrorActionPreference = 'Stop'
# Forwarded to install.ps1 when set.
$InstallParams = @{}
foreach ($name in 'Venv', 'Python', 'HermesHome') {
    if ($PSBoundParameters.ContainsKey($name)) { $InstallParams[$name] = $PSBoundParameters[$name] }
}
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Invoke-Git {
    $out = & git -C $Root @args
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed ($LASTEXITCODE)" }
    $out
}

function Write-Failure([string]$Message) { [Console]::Error.WriteLine("ERROR: $Message") }

function Test-Ancestor([string]$Ancestor, [string]$Descendant) {
    & git -C $Root merge-base --is-ancestor $Ancestor $Descendant
    $LASTEXITCODE -eq 0
}

function Find-Hermes {
    if ($InstallParams.ContainsKey('Venv')) {
        foreach ($rel in 'Scripts\hermes.exe', 'bin\hermes') {
            $candidate = Join-Path $InstallParams['Venv'] $rel
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    $cmd = Get-Command hermes -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Install-AndVerify([string]$Hermes) {
    # install.ps1 reports failure by throwing; Out-Host keeps its output out of the return value.
    try {
        & (Join-Path $Root 'install.ps1') -Yes @InstallParams | Out-Host
    } catch {
        Write-Warning $_
        return $false
    }
    if ($Hermes) {
        & $Hermes mnemosyne doctor --no-fix | Out-Host
        return ($LASTEXITCODE -eq 0)
    }
    Write-Host "NOTE: hermes not found; run 'hermes mnemosyne doctor --no-fix' yourself"
    return $true
}

function Invoke-Update {
    if ($Check -and $Apply) { Write-Failure 'choose -Check or -Apply, not both'; return 2 }
    Invoke-Git fetch --quiet $Remote $Branch | Out-Null
    $head = Invoke-Git rev-parse HEAD
    $remoteSha = Invoke-Git rev-parse FETCH_HEAD

    if ($head -eq $remoteSha -or (Test-Ancestor $remoteSha $head)) {
        if (-not $Quiet) { Write-Host "mnemosyne-hermes is up to date at $(Invoke-Git rev-parse --short HEAD)." }
        return 0
    }
    if (-not (Test-Ancestor $head $remoteSha)) {
        Write-Failure "local history has diverged from $Remote/$Branch; resolve it by hand"
        return 1
    }
    $count = Invoke-Git rev-list --count "$head..$remoteSha"
    Write-Host "Update available: $count new commit(s), $(Invoke-Git rev-parse --short $head) -> $(Invoke-Git rev-parse --short $remoteSha)"
    Invoke-Git log --oneline --max-count=20 "$head..$remoteSha" | Out-Host
    if (-not $Apply) {
        Write-Host 'Apply it with: scripts\update.ps1 -Apply'
        return 10
    }

    if ((Invoke-Git rev-parse --abbrev-ref HEAD) -ne $Branch) {
        Write-Failure "checkout is not on branch $Branch; refusing to update"
        return 1
    }
    if (Invoke-Git status --porcelain --untracked-files=no) {
        Write-Failure 'tracked files have local changes; refusing to update'
        return 1
    }

    $hermes = Find-Hermes
    Invoke-Git merge --ff-only --quiet $remoteSha | Out-Null
    if (Install-AndVerify $hermes) {
        Write-Host "Updated to $(Invoke-Git rev-parse --short HEAD). Restart the Hermes gateway to load it."
        return 0
    }
    Write-Warning "install or verification failed; restoring $(Invoke-Git rev-parse --short $head)"
    Invoke-Git reset --hard --quiet $head | Out-Null
    if (-not (Install-AndVerify $hermes)) {
        Write-Warning 'the restored install also failed verification; run install.ps1 by hand'
    }
    return 1
}

# One run at a time: a scheduler can fire again before a slow install finishes.
$lock = Join-Path (& git -C $Root rev-parse --absolute-git-dir) 'mnemosyne-update.lock'
try {
    New-Item -ItemType Directory -Path $lock -ErrorAction Stop | Out-Null
} catch {
    Write-Failure "another update is running (lock: $lock)"
    exit 1
}
try {
    # The last emitted value is the exit code; anything earlier would be stray output.
    $code = [int]@(Invoke-Update)[-1]
} catch {
    Write-Failure "$_"
    $code = 1
} finally {
    Remove-Item -LiteralPath $lock -Force -Recurse -ErrorAction SilentlyContinue
}
exit $code
