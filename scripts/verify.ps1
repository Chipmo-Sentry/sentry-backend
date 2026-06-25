# verify.ps1 — нэг командын merge-readiness gate (docs/31 §4.1).
#
# CI (.github/workflows/ci.yml)-тэй ижил шалгалтуудыг локалд ажиллуулна, дээр нь
# alembic-ийн ганц head-ийг шалгана (docs/31 §4.2 collision-аас сэргийлэх).
#
# Хэрэглээ:
#   pwsh scripts/verify.ps1
#
# Бүх алхам амжилттай бол exit 0; аль нэг нь унавал exit 1.

$ErrorActionPreference = "Stop"
$failed = @()

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    try {
        & $Action
        if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) {
            throw "exit code $LASTEXITCODE"
        }
        Write-Host "OK: $Name" -ForegroundColor Green
    } catch {
        Write-Host "FAIL: $Name -> $_" -ForegroundColor Red
        $script:failed += $Name
    }
}

Push-Location (Join-Path $PSScriptRoot "..")
try {
    Invoke-Step "Ruff — format check"     { uv run ruff format --check . }
    Invoke-Step "Ruff — lint"             { uv run ruff check . }
    Invoke-Step "Mypy — strict"           { uv run mypy src/sentry_backend }
    Invoke-Step "Pytest — unit"           {
        $env:DATABASE_URL        = "postgresql+asyncpg://noop:noop@localhost:5432/noop"
        $env:JWT_SECRET          = "ci-jwt-secret-32-characters-long-aaaaaaaa"
        $env:SERVICE_TOKEN_SECRET = "ci-service-token-secret-32-chars-aaaaa"
        $env:RTSP_FERNET_KEY     = "gvph5V5FZ4MN2cIk7rXhFpqpxCsAYAPOTaFL8qBIBSY="
        $env:ENVIRONMENT         = "dev"
        $env:LOG_LEVEL           = "WARNING"
        uv run pytest tests/unit/ -q
    }
    Invoke-Step "Alembic — exactly one head" {
        $heads = uv run alembic heads 2>&1
        Write-Host $heads
        $headLines = @($heads | Where-Object { $_ -match "\(head\)" })
        if ($headLines.Count -ne 1) {
            throw "Expected exactly 1 migration head, found $($headLines.Count). Merge/branch migration-уудыг нэгтгэ."
        }
    }
} finally {
    Pop-Location
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host "VERIFY FAILED: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "VERIFY PASSED — merge-ready" -ForegroundColor Green
exit 0
