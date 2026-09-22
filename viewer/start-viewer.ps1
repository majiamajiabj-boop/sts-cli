param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$viewerServer = Join-Path $PSScriptRoot 'server.py'

$viewerPython = $null
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $viewerPython = $pythonCommand.Source
}

if (-not $viewerPython) {
    $runtimePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $runtimePython) {
        $viewerPython = $runtimePython
    }
}

if (-not $viewerPython) {
    throw '未找到 Python。请从 Codex 中启动此看板，或安装 Python 3。'
}

& $viewerPython $viewerServer --root $repositoryRoot --host 127.0.0.1 --port $Port
