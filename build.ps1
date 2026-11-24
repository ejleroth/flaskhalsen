# Builds the portable Windows package: dist\Flaskhalsen-<version>.zip
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1
#
# Why not PyInstaller? Windows 11 Smart App Control blocks unsigned exe files it has never
# seen, and a freshly built PyInstaller exe is exactly that. Instead we ship the official
# Python embeddable package (signed by the Python Software Foundation) plus a .cmd launcher.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$pyVer = "3.12.10"
$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { python -m venv .venv; & $py -m pip install -r requirements.txt }
$version = (& $py -c "import flaskhalsen; print(flaskhalsen.__version__)").Trim()

$out = "dist\Flaskhalsen"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force "$out\runtime", "$out\app", "build" | Out-Null

# 1. Python runtime (cached between builds)
$embed = "build\python-$pyVer-embed-amd64.zip"
if (-not (Test-Path $embed)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-amd64.zip" -OutFile $embed
}
Expand-Archive $embed "$out\runtime"
# The ._pth file defines sys.path for the embedded interpreter: stdlib zip, runtime, site-packages.
"python312.zip`r`n.`r`nLib\site-packages`r`nimport site" | Set-Content -Encoding ascii "$out\runtime\python312._pth"

# 2. Dependencies (wheels for this exact Python/arch)
& $py -m pip install -q --target "$out\runtime\Lib\site-packages" --only-binary=:all: `
    --python-version 3.12 --platform win_amd64 -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip misslyckades" }
Remove-Item -Recurse -Force "$out\runtime\Lib\site-packages\pytest*", "$out\runtime\Lib\site-packages\_pytest", "$out\runtime\Lib\site-packages\bin" -ErrorAction SilentlyContinue

# 3. The app itself
Copy-Item run.py "$out\app\"
Copy-Item -Recurse config "$out\app\config"
Copy-Item -Recurse flaskhalsen "$out\app\flaskhalsen"
Get-ChildItem "$out\app" -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

Copy-Item packaging\Flaskhalsen.cmd "$out\"
Copy-Item packaging\LAS-MIG.txt "$out\"
Copy-Item README.md "$out\"

# 4. Single-file exe as an alternative download. It asks for administrator rights on start.
#    Note: Smart App Control still blocks it (unsigned); the zip above is the safe default.
& $py -m pip install -q pyinstaller==6.11.1
& $py -m PyInstaller --noconfirm --clean --onefile --uac-admin --name "Flaskhalsen-$version" `
    --distpath dist --workpath build\pyi --specpath build\pyi `
    --add-data "$PSScriptRoot\flaskhalsen\static;flaskhalsen\static" `
    --add-data "$PSScriptRoot\config;config" `
    --collect-submodules uvicorn `
    run.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller misslyckades" }

$zip = "dist\Flaskhalsen-$version.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $out -DestinationPath $zip
Write-Host "Klart: $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
$exe = "dist\Flaskhalsen-$version.exe"
Write-Host "Klart: $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB)"
