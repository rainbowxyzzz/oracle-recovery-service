param(
  [switch]$Strict
)

$ErrorActionPreference = "Stop"
$script:Errors = New-Object System.Collections.Generic.List[string]
$script:Warnings = New-Object System.Collections.Generic.List[string]
$script:Infos = New-Object System.Collections.Generic.List[string]

function Add-ErrorLine([string]$Message) { $script:Errors.Add($Message) }
function Add-WarningLine([string]$Message) { $script:Warnings.Add($Message) }
function Add-InfoLine([string]$Message) { $script:Infos.Add($Message) }

function Test-Port([string]$HostName, [int]$Port, [int]$TimeoutMs = 700) {
  try {
    $client = [System.Net.Sockets.TcpClient]::new()
    $async = $client.BeginConnect($HostName, $Port, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
      $client.Close()
      return $false
    }
    $client.EndConnect($async)
    $client.Close()
    return $true
  } catch {
    return $false
  }
}

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

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $ProjectRoot "extracted-app"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonPath = if ($env:PROJECT_PYTHON) { $env:PROJECT_PYTHON } elseif (Test-Path $VenvPython) { $VenvPython } else { Resolve-CommandPath "python" }

Write-Host "Project root: $ProjectRoot"
Write-Host "OS: $([System.Environment]::OSVersion.VersionString)"
Write-Host "Shell: PowerShell $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition))"

foreach ($requiredFile in @("AGENTS.md", ".env", "extracted-app\requirements.txt", "extracted-app\pyproject.toml")) {
  $path = Join-Path $ProjectRoot $requiredFile
  if (Test-Path $path) { Add-InfoLine "Found $requiredFile" } else { Add-ErrorLine "Missing required file: $requiredFile" }
}

if (Test-Path $VenvPython) {
  Add-InfoLine "Project virtualenv found: $VenvPython"
} else {
  Add-WarningLine "Project virtualenv missing: $VenvPython. Current machine has been using system Python; run scripts/bootstrap.ps1 to create the project venv."
}

if (-not $PythonPath -or -not (Test-Path $PythonPath)) {
  Add-ErrorLine "Python executable not found. Set PROJECT_PYTHON or run scripts/bootstrap.ps1 with a working Python >= 3.10."
} else {
  $py = Invoke-Capture $PythonPath @("-c", "import sys; print(sys.executable); print('.'.join(map(str, sys.version_info[:3]))); print(sys.prefix); print(sys.base_prefix)")
  if ($py.Code -ne 0) {
    Add-ErrorLine "Python check failed: $($py.Stderr)"
  } else {
    $lines = $py.Stdout -split "`r?`n"
    Add-InfoLine "Python executable: $($lines[0])"
    Add-InfoLine "Python version: $($lines[1])"
    $version = [version]$lines[1]
    if ($version -lt [version]"3.10.0") { Add-ErrorLine "Python version must be >= 3.10." }
    if ($lines[2] -eq $lines[3] -and -not (Test-Path $VenvPython)) { Add-WarningLine "Python is not running from a virtualenv." }
  }

  $runtimeModules = @(
    "fastapi", "uvicorn", "pydantic", "pydantic_settings", "sqlalchemy", "aiomysql",
    "pymysql", "alembic", "celery", "redis", "asyncssh", "paramiko", "oracledb",
    "structlog", "transitions", "httpx", "multipart", "yaml", "cryptography", "pypinyin",
    "openpyxl", "pyarrow"
  )
  $devModules = @("pytest", "pytest_asyncio", "ruff", "mypy")
  $moduleCode = "import importlib.util, sys; missing=[m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]; print('\n'.join(missing))"
  $missingRuntime = Invoke-Capture $PythonPath (@("-c", $moduleCode) + $runtimeModules)
  if ($missingRuntime.Stdout) {
    foreach ($module in ($missingRuntime.Stdout -split "`r?`n" | Where-Object { $_ })) { Add-ErrorLine "Missing required Python module: $module" }
  } else {
    Add-InfoLine "Required Python modules import successfully."
  }
  $missingDev = Invoke-Capture $PythonPath (@("-c", $moduleCode) + $devModules)
  if ($missingDev.Stdout) {
    foreach ($module in ($missingDev.Stdout -split "`r?`n" | Where-Object { $_ })) { Add-WarningLine "Missing dev Python module: $module" }
  } else {
    Add-InfoLine "Dev Python modules import successfully."
  }
}

$javaPath = Resolve-CommandPath "java"
$javacPath = if ($env:DORIS_SM4_JAVAC_BIN) { $env:DORIS_SM4_JAVAC_BIN } else { Resolve-CommandPath "javac" }
if ($env:JAVA_HOME) {
  if (Test-Path $env:JAVA_HOME) { Add-InfoLine "JAVA_HOME: $env:JAVA_HOME" } else { Add-WarningLine "JAVA_HOME is set but invalid: $env:JAVA_HOME" }
} else {
  Add-WarningLine "JAVA_HOME is not set. SM4 Java UDF packaging needs Java 8 compatible javac."
}
if ($javaPath) { Add-InfoLine "java: $javaPath" } else { Add-WarningLine "java command not found." }
if ($javacPath) {
  if (Test-Path $javacPath) { Add-InfoLine "javac: $javacPath" } else { Add-WarningLine "Configured javac path is invalid: $javacPath" }
} else {
  Add-WarningLine "javac command not found; set DORIS_SM4_JAVAC_BIN for SM4 UDF jar builds."
}

if ($env:SPARK_HOME) {
  if (Test-Path $env:SPARK_HOME) { Add-InfoLine "SPARK_HOME: $env:SPARK_HOME" } else { Add-WarningLine "SPARK_HOME is set but invalid: $env:SPARK_HOME" }
} else {
  Add-InfoLine "SPARK_HOME is not set; current project does not declare pyspark as a local requirement."
}
$sparkSubmit = Resolve-CommandPath "spark-submit"
if ($sparkSubmit) { Add-InfoLine "spark-submit: $sparkSubmit" } else { Add-InfoLine "spark-submit not found; do not treat this as a local unit-test failure." }

$nodePath = Resolve-CommandPath "node"
$npmPath = Resolve-CommandPath "npm"
if ($nodePath) {
  $nodeVersion = Invoke-Capture $nodePath @("--version")
  Add-InfoLine "Node.js: $nodePath $($nodeVersion.Stdout)"
} else {
  Add-WarningLine "node command not found; UI script checks need Node.js or the Codex bundled Node runtime."
}
if ($npmPath) { Add-InfoLine "npm: $npmPath" } else { Add-WarningLine "npm command not found. No package.json is currently used by this project." }

$dockerPath = Resolve-CommandPath "docker"
if ($dockerPath) {
  $dockerVersion = Invoke-Capture $dockerPath @("--version")
  Add-InfoLine "Docker CLI: $dockerPath $($dockerVersion.Stdout)"
  $dockerInfo = Invoke-Capture $dockerPath @("ps", "--format", "{{.Names}}")
  if ($dockerInfo.Code -ne 0) { Add-WarningLine "Docker daemon is not reachable (docker ps exit code $($dockerInfo.Code)); start Docker Desktop or use 128 for container verification." } else { Add-InfoLine "Docker daemon reachable." }
} else {
  Add-WarningLine "docker command not found; Docker Run packaging/deployment checks require Docker."
}

if ($env:ORACLE_HOME) {
  if (Test-Path $env:ORACLE_HOME) { Add-InfoLine "ORACLE_HOME: $env:ORACLE_HOME" } else { Add-WarningLine "ORACLE_HOME is set but invalid: $env:ORACLE_HOME" }
} else {
  Add-WarningLine "ORACLE_HOME is not set. Local unit tests do not need it; real impdp/sqlplus work should run in the configured Oracle host/container."
}
foreach ($tool in @("impdp", "sqlplus")) {
  $toolPath = Resolve-CommandPath $tool
  if ($toolPath) { Add-InfoLine "$tool`: $toolPath" } else { Add-WarningLine "$tool command not found locally." }
}

foreach ($entry in @(
  @{Name="MySQL metadata"; Port=3306},
  @{Name="Redis"; Port=6379},
  @{Name="API"; Port=8000},
  @{Name="Doris MySQL protocol"; Port=9030},
  @{Name="Doris FE HTTP"; Port=8030},
  @{Name="Doris BE HTTP"; Port=8040},
  @{Name="Oracle listener"; Port=1521}
)) {
  if (Test-Port "127.0.0.1" $entry.Port) { Add-InfoLine "127.0.0.1:$($entry.Port) reachable ($($entry.Name))." } else { Add-WarningLine "127.0.0.1:$($entry.Port) unreachable ($($entry.Name)); do not retry indefinitely if the service is expected to be remote/128-only." }
}

Write-Host ""
foreach ($line in $script:Infos) { Write-Host "INFO: $line" }
foreach ($line in $script:Warnings) { Write-Host "WARNING: $line" }
foreach ($line in $script:Errors) { Write-Host "ERROR: $line" }

Write-Host ""
if ($script:Errors.Count -eq 0 -and (-not $Strict -or $script:Warnings.Count -eq 0)) {
  Write-Host "Environment OK"
  exit 0
}
Write-Host "Environment check completed with $($script:Errors.Count) error(s) and $($script:Warnings.Count) warning(s)."
if ($script:Errors.Count -gt 0) { exit 1 }
if ($Strict -and $script:Warnings.Count -gt 0) { exit 2 }
exit 0
