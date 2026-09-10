param(
  [switch]$SkipDevDependencies,
  [string]$PipIndexUrl = "",
  [string]$PipTrustedHost = "",
  [int]$PipTimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"

function Resolve-CommandPath([string]$Name) {
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

function Invoke-Capture([string]$FilePath, [string[]]$Arguments) {
  $previousErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & $FilePath @Arguments 2>$null
    $code = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }
    $text = ($output | Out-String).Trim()
    return [pscustomobject]@{ Code = $code; Stdout = $text; Stderr = "" }
  } finally {
    $ErrorActionPreference = $previousErrorActionPreference
  }
}

function Test-PythonModules([string]$PythonExe, [string[]]$Modules) {
  $script = @'
import importlib.util
import sys
missing = [name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]
if missing:
    print("\n".join(missing))
    raise SystemExit(1)
'@
  $arguments = @("-c", $script) + $Modules
  $result = Invoke-Capture $PythonExe $arguments
  if ($result.Code -eq 0) { return @() }
  return @($result.Stdout -split "`r?`n" | Where-Object { $_ })
}

function Get-PipInstallOptions() {
  $options = @("install", "--disable-pip-version-check", "--default-timeout", "$PipTimeoutSeconds")
  if ($PipIndexUrl) { $options += @("-i", $PipIndexUrl) }
  if ($PipTrustedHost) { $options += @("--trusted-host", $PipTrustedHost) }
  return $options
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $ProjectRoot "extracted-app"
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$RequirementsFile = Join-Path $AppRoot "requirements.txt"
$PyProjectFile = Join-Path $AppRoot "pyproject.toml"

Write-Host "Project root: $ProjectRoot"
Write-Host "Bootstrap policy: project-local .venv only; no global Python/Java/Node/Docker changes."

if (-not (Test-Path $AppRoot)) { throw "extracted-app directory not found: $AppRoot" }
if (-not (Test-Path $RequirementsFile)) { throw "requirements.txt not found: $RequirementsFile" }
if (-not (Test-Path $PyProjectFile)) { throw "pyproject.toml not found: $PyProjectFile" }

if (-not (Test-Path $VenvPython)) {
  $BootstrapPython = if ($env:PROJECT_PYTHON) { $env:PROJECT_PYTHON } else { Resolve-CommandPath "python" }
  if (-not $BootstrapPython) { throw "python command not found. Install Python >=3.10 or set PROJECT_PYTHON to an existing interpreter." }

  $versionResult = Invoke-Capture $BootstrapPython @("-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
  if ($versionResult.Code -ne 0) { throw "Failed to run bootstrap Python: $($versionResult.Stderr)" }
  $version = [Version]$versionResult.Stdout
  if ($version -lt [Version]"3.10.0") { throw "Python >=3.10 is required, got $version from $BootstrapPython" }

  Write-Host "Creating project virtual environment: $VenvDir"
  & $BootstrapPython -m venv $VenvDir
} else {
  Write-Host "Virtual environment already exists: $VenvDir"
}

if (-not (Test-Path $VenvPython)) { throw "Virtual environment Python missing after creation: $VenvPython" }

$runtimeModules = @(
  "fastapi", "uvicorn", "pydantic", "pydantic_settings", "sqlalchemy", "aiomysql", "pymysql",
  "alembic", "celery", "redis", "asyncssh", "paramiko", "oracledb", "structlog", "transitions",
  "httpx", "multipart", "yaml", "cryptography", "pypinyin", "openpyxl", "pyarrow"
)
$missingRuntime = Test-PythonModules $VenvPython $runtimeModules
if ($missingRuntime.Count -gt 0) {
  Write-Host "Installing runtime dependencies from extracted-app\requirements.txt because modules are missing: $($missingRuntime -join ', ')"
  $pipArgs = (Get-PipInstallOptions) + @("-r", $RequirementsFile)
  & $VenvPython -m pip @pipArgs
} else {
  Write-Host "Runtime Python dependencies already import successfully."
}

if (-not $SkipDevDependencies) {
  $devModules = @("pytest", "pytest_asyncio", "ruff", "mypy")
  $missingDev = Test-PythonModules $VenvPython $devModules
  if ($missingDev.Count -gt 0) {
    Write-Host "Installing development dependencies because modules are missing: $($missingDev -join ', ')"
    $pipArgs = (Get-PipInstallOptions) + @("pytest>=8.3.0", "pytest-asyncio>=0.24.0", "ruff>=0.8.0", "mypy>=1.13.0")
    & $VenvPython -m pip @pipArgs
  } else {
    Write-Host "Development Python dependencies already import successfully."
  }
} else {
  Write-Host "Skipping development dependencies by request."
}

if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) {
  Write-Host "WARNING: .env is missing. Copy and fill the project environment template before running real API/Worker/database flows."
}

$CheckEnv = Join-Path $PSScriptRoot "check-env.ps1"
if (Test-Path $CheckEnv) {
  Write-Host "Running environment check after bootstrap..."
  & powershell -NoProfile -ExecutionPolicy Bypass -File $CheckEnv
}

Write-Host "Bootstrap completed."
Write-Host "Use: .\.venv\Scripts\Activate.ps1"
