# Offline test suite for Violet (Windows).
# Creates ..\.venv (workspace root) on first run, then runs pytest. Tests never contact WeChat,
# the LLM relay, MCP relays or a real database (see tests\conftest.py).
# First-run install is pinned by constraints-win-py311.txt (versions of the verified Windows venv).
param([Parameter(ValueFromRemainingArguments = $true)] $PytestArgs)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path (Split-Path -Parent $here) ".venv"
$python = Join-Path $venv "Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"

Push-Location $here
try {
    if (-not (Test-Path $python)) {
        $base = (Get-Command py -ErrorAction SilentlyContinue)
        if ($base) { & py -3.11 -m venv $venv } else { & python -m venv $venv }
        # Run from this folder so the nested "-r requirements.txt" resolves next to requirements-dev.txt.
        & $python -m pip install -r requirements-dev.txt -c constraints-win-py311.txt
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $python -m pytest @PytestArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
