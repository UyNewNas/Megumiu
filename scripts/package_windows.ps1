[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "Build the Windows portable archive on Windows."
}

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repositoryRoot "dist\windows"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$buildRoot = Join-Path $repositoryRoot ("build\portable-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $buildRoot, $OutputDirectory -Force | Out-Null

function Invoke-BuildPython {
    param([string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

Invoke-BuildPython -Arguments @("-c", "import platform, struct; assert struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64'), 'Use x64 Python'")
Invoke-BuildPython -Arguments @("-m", "pip", "install", "$repositoryRoot[camera]", "pyinstaller==6.22.3")
$versionOutput = & $Python -c "import importlib.metadata; print(importlib.metadata.version('megumiu'))"
if ($LASTEXITCODE -ne 0) { throw "Could not read the installed package version." }
$version = ($versionOutput | Select-Object -Last 1).Trim()
if ($version -notmatch '^[0-9][0-9A-Za-z.+-]*$') { throw "Invalid package version: $version" }

$distRoot = Join-Path $buildRoot "dist"
Invoke-BuildPython -Arguments @(
    "-m", "PyInstaller",
    "--noconfirm", "--onedir", "--console", "--name", "megumiu",
    "--distpath", $distRoot,
    "--workpath", (Join-Path $buildRoot "work"),
    "--specpath", $buildRoot,
    "--paths", (Join-Path $repositoryRoot "src"),
    "--copy-metadata", "megumiu",
    "--collect-all", "cv2",
    (Join-Path $repositoryRoot "packaging\windows_entry.py")
)

$portableDirectory = Join-Path $distRoot "megumiu"
$executable = Join-Path $portableDirectory "megumiu.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "PyInstaller did not produce megumiu.exe."
}
& $executable --help
if ($LASTEXITCODE -ne 0) { throw "Packaged executable --help failed." }
& $executable simulate --duration 2 --fps 60 --output (Join-Path $buildRoot "smoke")
if ($LASTEXITCODE -ne 0) { throw "Packaged executable simulation failed." }

foreach ($name in @("README.md", "LICENSE")) {
    $source = Join-Path $repositoryRoot $name
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        Copy-Item -LiteralPath $source -Destination $portableDirectory
    }
}
foreach ($name in @("configs", "docs")) {
    Copy-Item -LiteralPath (Join-Path $repositoryRoot $name) -Destination $portableDirectory -Recurse
}

$archivePath = Join-Path $OutputDirectory "megumiu-$version-windows-x64.zip"
Compress-Archive -LiteralPath $portableDirectory -DestinationPath $archivePath -Force
$checksum = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Portable archive: $archivePath"
Write-Host "SHA256: $checksum"
