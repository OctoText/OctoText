param(
    [string]$OctoPrintVersion = "2.0.1",
    [string]$PythonVersion = "3.12"
)

$ErrorActionPreference = "Stop"
$TestDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = Split-Path -Parent $TestDir
$VenvDir = Join-Path $TestDir ".venv-$OctoPrintVersion"

if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating venv for OctoPrint $OctoPrintVersion (Python $PythonVersion)..."
    uv venv --python $PythonVersion $VenvDir
    $extraPkgs = if ($OctoPrintVersion -like "1.*") { @("setuptools<81") } else { @() }
    uv pip install --python "$VenvDir\Scripts\python.exe" "octoprint==$OctoPrintVersion" pytest @extraPkgs
}

uv pip install --python "$VenvDir\Scripts\python.exe" -e $RepoDir
& "$VenvDir\Scripts\octoprint.exe" --version
& "$VenvDir\Scripts\pytest.exe" -v (Join-Path $TestDir "test_octotext.py")
