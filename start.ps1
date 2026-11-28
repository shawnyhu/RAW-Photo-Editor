$ErrorActionPreference = 'Stop'
$runId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$outDir = Join-Path $PSScriptRoot "results\$runId"
$null = New-Item -ItemType Directory -Path $outDir -Force
$portableDir = $null
$code = 1
try {
    $cpu = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($cpu -ne 'AMD64') { throw "Windows x64 is required. Detected $cpu." }
    $exe = $null
    $candidates = @(
        $env:RPE_BLENDER_EXE
        (Join-Path $env:ProgramFiles 'Blender Foundation\Blender 5.3\blender.exe')
        (Get-Command blender.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $python = Join-Path (Split-Path $candidate) '5.3\python\bin\python.exe'
            if (Test-Path -LiteralPath $python -PathType Leaf) {
                $exe = (Resolve-Path -LiteralPath $candidate).Path
                break
            }
        }
    }
    if (-not $exe) {
        Write-Host 'Downloading official portable Blender 5.3.0 to a temporary folder...'
        $portableDir = Join-Path $env:TEMP "rpe-blender-$runId"
        $null = New-Item -ItemType Directory -Path $portableDir
        $archive = Join-Path $portableDir 'blender.zip'
        $uri = 'https://download.blender.org/release/Blender5.3/blender-5.3.0-windows-x64.zip'
        & curl.exe --fail --location --silent --show-error --retry 3 --connect-timeout 30 --max-time 1800 --output $archive $uri
        if ($LASTEXITCODE -ne 0) { throw 'Could not download portable Blender. Set RPE_BLENDER_EXE to a local Blender 5.3 blender.exe and run again.' }
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne '3ac567a63d59d9934f7b9213bfb27ba05760e1fb366f8f55c381573ec3eabfa8') {
            throw 'Portable Blender checksum mismatch.'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $portableDir
        $exe = Join-Path $portableDir 'blender-5.3.0-windows-x64\blender.exe'
    }
    $python = Join-Path (Split-Path $exe) '5.3\python\bin\python.exe'
    & $python -I (Join-Path $PSScriptRoot 'run_tests.py') $exe $outDir
    $code = $LASTEXITCODE
    if (-not (Test-Path -LiteralPath (Join-Path $outDir 'report.json'))) {
        throw "The test runner exited before writing its report (exit $code)."
    }
} catch {
    $message = $_.Exception.Message
    Write-Host "FAIL: $message"
    [ordered]@{status='FAIL'; stage='bootstrap'; version='1.1.1'; error=$message} |
        ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $outDir 'report.json')
    $code = 1
} finally {
    if ($portableDir -and (Test-Path -LiteralPath $portableDir)) {
        Remove-Item -LiteralPath $portableDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
try {
    $evidence = Join-Path $PSScriptRoot "results\raw_photo_editor-1.1.1-windows-$runId.zip"
    Compress-Archive -Path (Join-Path $outDir '*') -DestinationPath $evidence
    Write-Host "Evidence: $evidence"
} catch {
    Write-Host "Could not zip the report. Evidence directory: $outDir"
}
exit $code
