param(
    [string]$ConfigPath = "config/media-config.properties",
    [string[]]$VideoIds = @(),
    [string]$Quality = "",
    [string]$OutputFormat = "",
    [string]$JobsCsv = "",
    [switch]$StopOnError
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonScript = Join-Path $projectRoot "scripts\yt_download_audio.py"

if (-not (Test-Path $pythonScript)) {
    throw "Script not found: $pythonScript"
}

# Resolve config path relative to project root when not absolute.
if (-not [System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath = Join-Path $projectRoot $ConfigPath
}

if (-not (Test-Path $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

function Invoke-Download {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VideoId,
        [string]$StartTime = "",
        [string]$EndTime = ""
    )

    if ([string]::IsNullOrWhiteSpace($VideoId)) {
        return
    }

    $cmdParts = New-Object System.Collections.Generic.List[string]
    [void]$cmdParts.Add($pythonScript)
    [void]$cmdParts.Add("--config")
    [void]$cmdParts.Add($ConfigPath)
    [void]$cmdParts.Add("--video-id")
    [void]$cmdParts.Add($VideoId)

    if (-not [string]::IsNullOrWhiteSpace($Quality)) {
        [void]$cmdParts.Add("--quality")
        [void]$cmdParts.Add($Quality)
    }
    if (-not [string]::IsNullOrWhiteSpace($OutputFormat)) {
        [void]$cmdParts.Add("--output-format")
        [void]$cmdParts.Add($OutputFormat)
    }

    if (-not [string]::IsNullOrWhiteSpace($StartTime)) {
        [void]$cmdParts.Add("--start")
        [void]$cmdParts.Add($StartTime)
    }
    if (-not [string]::IsNullOrWhiteSpace($EndTime)) {
        [void]$cmdParts.Add("--end")
        [void]$cmdParts.Add($EndTime)
    }

    Write-Host "Running: python $($cmdParts -join ' ')" -ForegroundColor Cyan
    & python ($cmdParts.ToArray())
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        Write-Host "Failed videoId=$VideoId (exit=$exitCode)" -ForegroundColor Red
        if ($StopOnError) {
            exit $exitCode
        }
    } else {
        Write-Host "Completed videoId=$VideoId" -ForegroundColor Green
    }
}

if (-not [string]::IsNullOrWhiteSpace($JobsCsv)) {
    if (-not [System.IO.Path]::IsPathRooted($JobsCsv)) {
        $JobsCsv = Join-Path $projectRoot $JobsCsv
    }

    if (-not (Test-Path $JobsCsv)) {
        throw "Jobs CSV not found: $JobsCsv"
    }

    # CSV columns: videoId,startTime,endTime
    $jobs = Import-Csv -Path $JobsCsv
    foreach ($job in $jobs) {
        Invoke-Download -VideoId $job.videoId -StartTime $job.startTime -EndTime $job.endTime
    }
    exit 0
}

if ($VideoIds.Count -eq 0) {
    throw "Provide -VideoIds or -JobsCsv. Example: -VideoIds Py8Z7D15JYo,C38Ov5g5e7c"
}

foreach ($id in $VideoIds) {
    Invoke-Download -VideoId $id
}
