[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$Copy,
    [switch]$Uninstall,
    [switch]$Purge,
    [string]$Venv,
    [string]$Python,
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'hermes' } else { Join-Path $HOME '.hermes' }),
    [string]$DbPath = $env:MNEMOSYNE_DB_PATH
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProviderSource = Join-Path $Root 'integrations/hermes-provider'
$Provider = Join-Path $ProviderSource 'hermes_memory_provider'
$Plugin = Join-Path $HermesHome 'plugins/mnemosyne'
$Config = Join-Path $HermesHome 'config.yaml'
$DataRoot = Join-Path $HermesHome 'mnemosyne'
$EnginePin = 'mnemosyne-memory[embeddings]>=3.15.1,<3.16'
$env:HERMES_HOME = $HermesHome

function Fail([string]$Message) { throw "ERROR: $Message" }
function Say([string]$Message) { Write-Host "  $Message" }

function Invoke-PythonCode([string]$Code, [string[]]$Arguments = @()) {
    # PowerShell 5.1 drops embedded quotes when passing multiline -c code to a
    # native executable. Execute a short-lived UTF-8 script file instead.
    $scriptPath = [IO.Path]::Combine([IO.Path]::GetTempPath(), [Guid]::NewGuid().ToString('N') + '.py')
    [IO.File]::WriteAllText($scriptPath, $Code, [Text.UTF8Encoding]::new($false))
    try {
        $output = & $VenvPython $scriptPath @Arguments
        $script:PythonExitCode = $LASTEXITCODE
        return $output
    } finally {
        Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-Python([string]$HermesRoot, [string]$RequestedVenv, [string]$RequestedPython) {
    if ($RequestedPython) {
        if (-not (Test-Path -LiteralPath $RequestedPython -PathType Leaf)) { Fail "--Python does not exist: $RequestedPython" }
        return (Resolve-Path -LiteralPath $RequestedPython).Path
    }
    $candidates = @()
    if ($RequestedVenv) { $candidates += $RequestedVenv }
    if ($env:HERMES_VENV) { $candidates += $env:HERMES_VENV }
    $candidates += (Join-Path $HermesRoot 'venv'), (Join-Path $HermesRoot 'hermes-agent/venv'), (Join-Path $HermesRoot 'hermes-agent/.venv')
    $cmd = Get-Command hermes -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        $bin = Split-Path -Parent $cmd.Source
        if ((Split-Path -Leaf $bin) -in @('Scripts', 'bin')) { $candidates += Split-Path -Parent $bin }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        foreach ($pythonPath in @((Join-Path $candidate 'Scripts/python.exe'), (Join-Path $candidate 'bin/python'))) {
            if (Test-Path -LiteralPath $pythonPath -PathType Leaf) { return (Resolve-Path -LiteralPath $pythonPath).Path }
        }
    }
    Fail 'could not find the Hermes Python; pass -Venv or -Python'
}

function Get-FileDigest([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try { $raw = [Security.Cryptography.SHA256]::Create().ComputeHash($stream) }
    finally { $stream.Dispose() }
    return [Convert]::ToBase64String($raw).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Get-Inventory([string]$Directory) {
    $inventory = [ordered]@{}
    $items = Get-ChildItem -LiteralPath $Directory -Force -Recurse
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "provider copy contains a reparse point: $($item.FullName)" }
    }
    $items | Where-Object {
        -not $_.PSIsContainer -and
        $_.Name -ne 'PROVENANCE.json' -and $_.FullName -notmatch '[\\/]__pycache__[\\/]'
    } | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($Directory.TrimEnd('\').Length + 1).Replace('\', '/')
        if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "provider copy contains a link: $($_.FullName)" }
        $inventory[$relative] = Get-FileDigest $_.FullName
    }
    return $inventory
}

function Test-InstallerCopy([string]$Directory) {
    $provenancePath = Join-Path $Directory 'PROVENANCE.json'
    if (-not (Test-Path -LiteralPath $provenancePath -PathType Leaf)) { return $false }
    try {
        $metadata = Get-Content -LiteralPath $provenancePath -Raw | ConvertFrom-Json
        $source = [IO.Path]::GetFullPath([string]$metadata.copied_from).TrimEnd('\')
        if ($metadata.format_version -ne 1 -or $source -ne [IO.Path]::GetFullPath($Provider).TrimEnd('\')) { return $false }
        $actual = Get-Inventory $Directory
        $expected = [ordered]@{}
        foreach ($property in $metadata.files.PSObject.Properties) { $expected[$property.Name] = [string]$property.Value }
        if ($actual.Count -ne $expected.Count) { return $false }
        foreach ($key in $actual.Keys) { if ($expected[$key] -ne $actual[$key]) { return $false } }
        return $true
    } catch { return $false }
}

function Test-ReparsePoint([string]$Path) {
    return [bool]((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Assert-NoReparseTraversal([string]$Path) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    $current = $root
    foreach ($part in $fullPath.Substring($root.Length).Split([IO.Path]::DirectorySeparatorChar, [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current -ErrorAction SilentlyContinue) {
            if (Test-ReparsePoint $current) { Fail "refusing to use a path through reparse point: $current" }
        }
    }
}

function Test-InstallerLink([string]$Path) {
    try {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.LinkType -ne 'SymbolicLink') { return $false }
        $target = [string]($item.Target | Select-Object -First 1)
        return ([IO.Path]::GetFullPath((Join-Path $item.DirectoryName $target)).TrimEnd('\') -eq [IO.Path]::GetFullPath($Provider).TrimEnd('\'))
    } catch { return $false }
}

function Write-Provenance([string]$Directory) {
    $payload = [ordered]@{
        format_version = 1
        copied_from = [IO.Path]::GetFullPath($Provider)
        copied_at = [DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
        hash_algorithm = 'sha256, base64url without padding (wheel RECORD format)'
        files = Get-Inventory $Directory
    }
    $json = $payload | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText(
        (Join-Path $Directory 'PROVENANCE.json'),
        $json,
        [Text.UTF8Encoding]::new($false)
    )
}

function Read-ConfigProvider {
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { return '' }
    $reader = @'
import pathlib, sys, yaml
try:
    data = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
except Exception:
    raise SystemExit(2)
memory = data.get("memory") if isinstance(data, dict) else None
provider = memory.get("provider", "") if isinstance(memory, dict) else ""
print(provider if isinstance(provider, str) else "")
'@
    $value = Invoke-PythonCode $reader @($Config)
    if ($script:PythonExitCode -ne 0) { return '' }
    return [string]($value | Select-Object -Last 1)
}

function Assert-SafeChildTarget([string]$Path, [string]$Anchor) {
    $fullAnchor = [IO.Path]::GetFullPath($Anchor).TrimEnd('\\')
    $fullTarget = [IO.Path]::GetFullPath($Path)
    $prefix = $fullAnchor + '\'
    if (-not $fullTarget.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { Fail "refusing recursive operation outside ${Anchor}: $fullTarget" }
    $ancestor = $fullAnchor
    while ($ancestor) {
        if ((Test-Path -LiteralPath $ancestor) -and (Test-ReparsePoint $ancestor)) { Fail "refusing path traversal through reparse point: $ancestor" }
        $parent = [IO.Directory]::GetParent($ancestor)
        if (-not $parent -or $parent.FullName -eq $ancestor) { break }
        $ancestor = $parent.FullName
    }
    $current = $fullAnchor
    foreach ($part in $fullTarget.Substring($prefix.Length).Split('\')) {
        $current = Join-Path $current $part
        if ((Test-Path -LiteralPath $current) -and (Test-ReparsePoint $current)) { Fail "refusing path traversal through reparse point: $current" }
    }
    return $fullTarget
}

function Test-ProviderRegistration {
    $verifier = @'
import importlib.util, os, sys
provider_dir = os.path.realpath(sys.argv[1])
spec = importlib.util.spec_from_file_location("_hermes_user_memory.mnemosyne",
    os.path.join(provider_dir, "__init__.py"), submodule_search_locations=[provider_dir])
if spec is None or spec.loader is None: raise SystemExit("cannot load provider")
module = importlib.util.module_from_spec(spec)
sys.modules["_hermes_user_memory.mnemosyne"] = module
spec.loader.exec_module(module)
cli_path = os.path.join(provider_dir, "cli.py")
cli_src = open(cli_path, encoding="utf-8", errors="replace").read() if os.path.exists(cli_path) else ""
for name in ("register_cli", "mnemosyne_command"):
    if f"def {name}" not in cli_src: raise SystemExit(f"cli.py is missing {name}")
providers, commands = [], []
class Ctx:
    def register_memory_provider(self, provider): providers.append(provider)
    def register_cli_command(self, **kwargs): commands.append(kwargs)
    def register_tool(self, *args, **kwargs): pass
    def register_hook(self, *args, **kwargs): pass
module.register(Ctx())
if len(providers) != 1: raise SystemExit(f"expected exactly one provider, got {len(providers)}")
names = [command.get("name") for command in commands]
if names != ["mnemosyne"]: raise SystemExit(f"expected exactly one mnemosyne CLI command, got {names}")
if not callable(commands[0].get("setup_fn")) or not callable(commands[0].get("handler_fn")):
    raise SystemExit("mnemosyne CLI command must declare setup_fn and handler_fn")
provider = providers[0]
if provider.name != "mnemosyne" or not provider.is_available():
    reason = provider.unavailable_reason() if hasattr(provider, "unavailable_reason") else ""
    raise SystemExit(f"provider unavailable or misnamed: {reason}")
print("provider registered: mnemosyne (available); exactly one provider and CLI command")
'@
    Invoke-PythonCode $verifier @($Plugin) | Out-Null
    return ($script:PythonExitCode -eq 0)
}

function Test-HermesLoaderSelection {
    # Validate the loader's actual bundled/user precedence and the class it
    # instantiates. A direct import of the copied directory is insufficient:
    # Hermes deliberately lets a bundled same-name provider win collisions.
    $verifier = @'
import inspect, pathlib, sys
try:
    from plugins.memory import find_provider_dir, load_memory_provider
except Exception as exc:
    raise SystemExit(f"cannot access Hermes memory-provider loader: {exc}")
expected = pathlib.Path(sys.argv[1]).resolve()
selected = find_provider_dir("mnemosyne")
if selected is None or pathlib.Path(selected).resolve() != expected:
    raise SystemExit(f"Hermes resolves mnemosyne to {selected}, expected deployed provider {expected}")
provider = load_memory_provider("mnemosyne")
if provider is None:
    raise SystemExit("Hermes loader could not instantiate mnemosyne")
provider_file = pathlib.Path(inspect.getfile(type(provider))).resolve()
try:
    provider_file.relative_to(expected)
except ValueError:
    raise SystemExit(f"Hermes instantiated {provider_file}, outside deployed provider {expected}")
print(f"Hermes loader selected deployed provider class: {provider_file}")
'@
    Invoke-PythonCode $verifier @($Plugin) | Out-Null
    return ($script:PythonExitCode -eq 0)
}

function Test-EngineImport {
    Invoke-PythonCode 'import mnemosyne.core.beam' @() | Out-Null
    if ($script:PythonExitCode -ne 0) { return $false }
    $engineFile = Invoke-PythonCode 'import mnemosyne,os; print(os.path.realpath(mnemosyne.__file__))' @()
    if ($script:PythonExitCode -ne 0 -or -not $engineFile) { return $false }
    $repoSource = [IO.Path]::GetFullPath((Join-Path $Root 'src')).TrimEnd('\\') + '\'
    return -not ([IO.Path]::GetFullPath(([string]$engineFile).Trim()).StartsWith($repoSource, [StringComparison]::OrdinalIgnoreCase))
}

function Test-PackageInstalled([string]$PackageName) {
    Invoke-PythonCode 'import importlib.metadata,sys; importlib.metadata.distribution(sys.argv[1])' @($PackageName) | Out-Null
    return ($script:PythonExitCode -eq 0)
}

function Uninstall-Package([string]$PackageName) {
    if (-not (Test-PackageInstalled $PackageName)) { Say "$PackageName is not installed; skipping"; return }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { & $uv.Source pip uninstall --python $VenvPython $PackageName *> $null }
    else { & $VenvPython -m pip uninstall -y $PackageName *> $null }
    if ($LASTEXITCODE -ne 0) { Fail "could not uninstall $PackageName from $VenvPython" }
    if (Test-PackageInstalled $PackageName) { Fail "$PackageName still appears installed after uninstall" }
}

function Install-LegacyPackages {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { & $uv.Source pip install --python $VenvPython $ProviderSource $EnginePin }
    else { & $VenvPython -m pip install $ProviderSource $EnginePin }
    if ($LASTEXITCODE -ne 0) { Fail 'provider/engine installation failed' }
    & $VenvPython (Join-Path $Root 'scripts/apply_engine_patches.py')
    if ($LASTEXITCODE -ne 0) { Fail 'audited engine patches could not be applied' }
}

function Invoke-Hermes([string[]]$Arguments) {
    & $script:HermesCommand @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "Hermes command failed ($LASTEXITCODE): hermes $($Arguments -join ' ')" }
}

function Resolve-PmPython {
    # Hermes facts.json is the current PM selection record. Requiring the
    # resolved interpreter to remain under this install's environments folder
    # prevents patching a different Python or a user-supplied path.
    $projectRoot = Split-Path -Parent $Venv
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'pm/environments.py'))) { Fail "could not identify the Hermes PM checkout beside $Venv" }
    $keyCode = 'import hashlib,pathlib,sys; print(hashlib.sha256(str(pathlib.Path(sys.argv[1]).resolve()).encode("utf-8")).hexdigest()[:16])'
    $key = Invoke-PythonCode $keyCode @($projectRoot)
    if ($script:PythonExitCode -ne 0 -or -not $key) { Fail 'could not derive the Hermes install key' }
    $state = Join-Path (Join-Path $HermesHome 'installs') $key.Trim()
    $factsPath = Join-Path $state 'facts.json'
    Assert-NoReparseTraversal $factsPath
    if (-not (Test-Path -LiteralPath $factsPath -PathType Leaf)) { Fail "Hermes PM selection is missing: $factsPath" }
    try {
        $facts = Get-Content -LiteralPath $factsPath -Raw | ConvertFrom-Json
        $environment = [IO.Path]::GetFullPath([string]$facts.packages.venv.environment)
        $envRoot = [IO.Path]::GetFullPath((Join-Path $state 'environments')).TrimEnd('\') + '\'
        if (-not $environment.StartsWith($envRoot, [StringComparison]::OrdinalIgnoreCase)) { Fail 'Hermes PM selected environment escapes its install state' }
        Assert-NoReparseTraversal $environment
        $realpathCode = 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=True))'
        $resolvedEnvironment = Invoke-PythonCode $realpathCode @($environment)
        $resolvedEnvRoot = Invoke-PythonCode $realpathCode @((Join-Path $state 'environments'))
        if ($script:PythonExitCode -ne 0 -or -not $resolvedEnvironment -or -not $resolvedEnvRoot) { Fail 'could not resolve Hermes PM environment boundaries' }
        $resolvedEnvPrefix = [IO.Path]::GetFullPath(([string]($resolvedEnvRoot | Select-Object -Last 1)).Trim()).TrimEnd('\') + '\'
        $resolvedEnvironment = [IO.Path]::GetFullPath(([string]($resolvedEnvironment | Select-Object -Last 1)).Trim())
        if (-not $resolvedEnvironment.StartsWith($resolvedEnvPrefix, [StringComparison]::OrdinalIgnoreCase)) { Fail 'Hermes PM selected environment resolves outside its install state' }
        $selectedPython = Join-Path $environment 'Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $selectedPython -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $environment 'pyvenv.cfg') -PathType Leaf)) { Fail 'Hermes PM selected environment is incomplete' }
        return (Resolve-Path -LiteralPath $selectedPython).Path
    } catch { Fail "could not resolve Hermes PM selected Python: $_" }
}

$VenvPython = Resolve-Python $HermesHome $Venv $Python
$Venv = Split-Path -Parent (Split-Path -Parent $VenvPython)
$HermesCommand = $null
$selectedHermes = Join-Path $Venv 'Scripts/hermes.exe'
if (Test-Path -LiteralPath $selectedHermes) { $HermesCommand = $selectedHermes }
else {
    $found = Get-Command hermes -ErrorAction SilentlyContinue
    if ($found) { $HermesCommand = $found.Source }
}

$HermesPM = $false
if ($HermesCommand) {
    $projectCandidate = Split-Path -Parent $Venv
    if ((Test-Path -LiteralPath (Join-Path $projectCandidate 'pm/cli.py')) -or (Test-Path -LiteralPath (Join-Path $HermesHome 'hermes-agent/pm/cli.py'))) {
        $HermesPM = $true
    } elseif (-not $DryRun) {
        & $HermesCommand pm status *> $null
        $HermesPM = ($LASTEXITCODE -eq 0)
    }
}

if (-not $DbPath) { $DbPath = Join-Path $DataRoot 'data/mnemosyne.db' }

if ($Uninstall) {
    $Plugin = Assert-SafeChildTarget $Plugin (Join-Path $HermesHome 'plugins')
    if ($Purge) { $DataRoot = Assert-SafeChildTarget $DataRoot $HermesHome }
    if ((Test-Path -LiteralPath $Plugin) -and (Test-ReparsePoint $Plugin) -and -not (Test-InstallerLink $Plugin)) { Fail "refusing to remove ${Plugin}: link target is not the canonical provider source" }
    if ((Test-Path -LiteralPath $Plugin) -and -not (Test-ReparsePoint $Plugin) -and -not (Test-InstallerCopy $Plugin)) {
        Fail "refusing to remove ${Plugin}: it is not an intact installer-created copy"
    }
    Write-Host 'Hermes provider uninstall plan'
    Write-Host "  Hermes home      : $HermesHome"
    Write-Host "  Python           : $VenvPython"
    Write-Host "  plugin copy      : $Plugin (removed)"
    Write-Host "  dependency owner : $(if ($HermesPM) { 'Hermes PM' } else { 'installer pip/uv' })"
    Write-Host "  memory DB path   : $DbPath"
    if ($Purge) { Write-Host "  purge            : removes $DataRoot" } else { Write-Host '  memory data      : kept (pass -Purge to remove it)' }
    if ($DryRun) { Write-Host "`n-DryRun: no changes made."; exit 0 }
    if (-not $Yes) {
        if (-not [Console]::IsInputRedirected) { if ((Read-Host 'Proceed? [y/N]') -notmatch '^(y|yes)$') { Fail 'cancelled; nothing changed' } }
        else { Fail 'refusing to uninstall without confirmation (pass -Yes, or -DryRun to inspect)' }
    }
    if ($HermesCommand -and (Read-ConfigProvider) -eq 'mnemosyne') {
        Invoke-Hermes @('config', 'set', 'memory.provider', '')
        if ($HermesPM) { Invoke-Hermes @('pm', 'install') }
    }
    if (Test-Path -LiteralPath $Plugin) {
        if (Test-ReparsePoint $Plugin) { Remove-Item -LiteralPath $Plugin -Force }
        else { Remove-Item -LiteralPath $Plugin -Recurse -Force }
    }
    if (-not $HermesPM) {
        Uninstall-Package 'mnemosyne-hermes-provider'
        if ($Purge) { Uninstall-Package 'mnemosyne-memory' }
    }
    if ($Purge -and (Test-Path -LiteralPath $DataRoot)) {
        $resolved = Assert-SafeChildTarget $DataRoot $HermesHome
        if ($resolved -eq [IO.Path]::GetPathRoot($resolved)) { Fail "refusing unsafe purge target: $resolved" }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    Write-Host 'Uninstalled. Restart Hermes so it stops running the provider module.'
    exit 0
}

if (-not (Test-Path -LiteralPath $Provider -PathType Container)) { Fail "vendored provider missing: $Provider" }
Assert-SafeChildTarget $Plugin (Join-Path $HermesHome 'plugins') | Out-Null
if (-not $HermesCommand -and (Test-Path -LiteralPath $Config)) { Fail 'cannot select memory.provider: no Hermes CLI was found; pass the correct -Venv/-Python' }

Write-Host 'Hermes provider install plan'
Write-Host "  provider source  : $Provider"
Write-Host "  Python           : $VenvPython"
Write-Host "  plugin copy      : $Plugin (copy; no symlink privilege required)"
Write-Host "  config           : $Config (memory.provider=mnemosyne)"
Write-Host "  memory DB path   : $DbPath (not opened or created by this script)"
Write-Host "  engine pin       : $EnginePin"
Write-Host "  dependency owner : $(if ($HermesPM) { 'Hermes PM' } else { 'installer pip/uv' })"
if ($DryRun) { Write-Host "`n-DryRun: no changes made."; exit 0 }
if (-not $Yes) {
    if (-not [Console]::IsInputRedirected) { if ((Read-Host 'Proceed? [y/N]') -notmatch '^(y|yes)$') { Fail 'cancelled; nothing changed' } }
    else { Fail 'refusing to install without confirmation (pass -Yes, or -DryRun to inspect)' }
}

if ((Test-Path -LiteralPath $Plugin) -and (Test-ReparsePoint $Plugin) -and -not (Test-InstallerLink $Plugin)) { Fail "${Plugin} exists as a reparse point to a different target; refusing to overwrite it" }
if ((Test-Path -LiteralPath $Plugin) -and -not (Test-ReparsePoint $Plugin) -and -not (Test-InstallerCopy $Plugin)) {
    Fail "${Plugin} exists but is not an intact installer-created copy; refusing to overwrite it"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Plugin) | Out-Null
if (Test-Path -LiteralPath $Plugin) {
    if (Test-ReparsePoint $Plugin) { Remove-Item -LiteralPath $Plugin -Force }
    else { Remove-Item -LiteralPath $Plugin -Recurse -Force }
}
Copy-Item -LiteralPath $Provider -Destination $Plugin -Recurse
Write-Provenance $Plugin

if ($HermesPM) {
    Invoke-Hermes @('config', 'set', 'memory.provider', 'mnemosyne')
    Invoke-Hermes @('pm', 'install')
    $VenvPython = Resolve-PmPython
    & $VenvPython (Join-Path $Root 'scripts/apply_engine_patches.py')
    if ($LASTEXITCODE -ne 0) { Fail 'audited engine patches could not be applied to the admitted Hermes PM environment' }
} else {
    Install-LegacyPackages
}

if (-not $HermesPM) { Invoke-Hermes @('config', 'set', 'memory.provider', 'mnemosyne') }
if ((Read-ConfigProvider) -ne 'mnemosyne') { Fail 'Hermes config does not select memory.provider=mnemosyne' }
if (-not (Test-Path -LiteralPath (Join-Path $Plugin '__init__.py'))) { Fail "$Plugin has no __init__.py" }
if (-not (Test-InstallerCopy $Plugin)) { Fail 'installed plugin copy failed provenance verification' }
if (-not (Test-ProviderRegistration)) { Fail 'installed plugin does not expose exactly one available provider and CLI command' }
if (-not (Test-EngineImport)) { Fail "mnemosyne-memory engine cannot be imported from $VenvPython" }
if (-not (Test-HermesLoaderSelection)) { Fail 'Hermes memory-provider loader does not resolve mnemosyne to the deployed provider class' }
Write-Host "`nInstalled. Restart Hermes so it loads the provider."
Write-Host "Database (created on first write): $DbPath"
