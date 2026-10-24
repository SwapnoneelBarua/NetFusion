$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $project
try {
    $appVersion = (Get-Content -LiteralPath (Join-Path $project "VERSION") -Raw).Trim()
    if ($appVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "VERSION must contain a stable numeric semantic version (for example 1.0.0). Found: $appVersion"
    }

    # Build with Python 3.14 and the pinned Qt 6.9.3 wheel. Qt 6.10+ in this
    # environment has an ICU dependency that can bind to incompatible external
    # ICU DLLs on PATH.
    $buildVenv = Join-Path $project ".venv-build314"
    $python = Join-Path $buildVenv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source venv $buildVenv --python 3.14
        } else {
            & py -3.14 -m venv $buildVenv
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
            throw "Python 3.14 is required for a reliable Qt build. Install Python 3.14 or uv with its Python 3.14 runtime, then rerun this script."
        }
    }

    $pythonVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.14") {
        throw "The build environment must use Python 3.14; found $pythonVersion. Remove .venv-build314 and rerun this script."
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & $uv.Source pip install --python $python -r requirements.txt -r requirements-build.txt
    } else {
        & $python -m pip install -r requirements.txt -r requirements-build.txt
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not install build requirements." }

    # Keep unrelated native tool DLLs (for example, Conda or Poppler ICU)
    # off PyInstaller's analysis PATH so they cannot be copied into the app.
    $originalPath = $env:PATH
    $pythonBase = & $python -c "import sys; print(sys.base_prefix)"
    $versionInfoPath = Join-Path $project "tools\version_info.generated.txt"
    $versionParts = $appVersion.Split('.') | ForEach-Object { [int]$_ }
    $fileVersion = "($($versionParts[0]), $($versionParts[1]), $($versionParts[2]), 0)"
    $versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=$fileVersion,
    prodvers=$fileVersion,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'NetFusion'),
        StringStruct('FileDescription', 'NetFusion multi-path proxy'),
        StringStruct('FileVersion', '$appVersion'),
        StringStruct('InternalName', 'NetFusion'),
        StringStruct('OriginalFilename', 'NetFusion.exe'),
        StringStruct('ProductName', 'NetFusion'),
        StringStruct('ProductVersion', '$appVersion')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
    Set-Content -LiteralPath $versionInfoPath -Value $versionInfo -Encoding utf8
    try {
        $env:PATH = "$buildVenv\Scripts;$pythonBase;$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
        & $python .\tools\build_icon.py
        if ($LASTEXITCODE -ne 0) { throw "Could not build the Windows application icon." }
        & $python -m PyInstaller --noconfirm --clean --windowed --onedir `
            --contents-directory _internal --runtime-hook .\src\qt_dll_bootstrap.py `
            --icon .\icon\netfusion.ico --version-file $versionInfoPath `
            --add-data ".\icon\icon.png;icon" --add-data ".\VERSION;." `
            --name NetFusion .\src\main.py
    } finally {
        $env:PATH = $originalPath
    }
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    $unexpectedIcu = Join-Path $project "dist\NetFusion\_internal\icuuc.dll"
    if (Test-Path -LiteralPath $unexpectedIcu) {
        throw "Unexpected ICU DLL was bundled from the build environment. Check the isolated build PATH before distributing."
    }

    $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if (-not $compiler) {
        $candidates = @(
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        )
        $compilerPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    } else {
        $compilerPath = $compiler.Source
    }
    if (-not $compilerPath) {
        throw "Inno Setup is required. Install it, then rerun this script. Download: https://jrsoftware.org/isdl.php"
    }

    & $compilerPath "/DAppVersion=$appVersion" .\installer\NetFusion.iss
    if ($LASTEXITCODE -ne 0) { throw "Installer compilation failed." }
    Write-Host "Installer created: $project\dist\installer\NetFusion-Setup-x64.exe"
} finally {
    Pop-Location
}
