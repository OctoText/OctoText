param(
    [string]$OctoPrintVersion = "2.0.1",
    [string]$PythonVersion = "3.12",
    [int]$Port = 5000
)

$ErrorActionPreference = "Stop"
$TestDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = Split-Path -Parent $TestDir
$VenvDir = Join-Path $TestDir ".venv-$OctoPrintVersion"
$BaseDir = Join-Path $TestDir ".basedir-$OctoPrintVersion"
$CertFile = Join-Path $TestDir "smtp.crt"
$KeyFile = Join-Path $TestDir "smtp.key"

if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating venv for OctoPrint $OctoPrintVersion (Python $PythonVersion)..."
    uv venv --python $PythonVersion $VenvDir
    $extraPkgs = if ($OctoPrintVersion -like "1.*") { @("setuptools<81") } else { @() }
    uv pip install --python "$VenvDir\Scripts\python.exe" "octoprint==$OctoPrintVersion" pytest @extraPkgs
}

Write-Host "Installing OctoText from $RepoDir (editable)..."
uv pip install --python "$VenvDir\Scripts\python.exe" -e $RepoDir

if (-not (Test-Path $CertFile) -or -not (Test-Path $KeyFile)) {
    $openssl = "C:\Program Files\Git\usr\bin\openssl.exe"
    if (-not (Test-Path $openssl)) {
        throw "OpenSSL not found at $openssl. Install Git for Windows or generate smtp.crt/smtp.key manually."
    }
    Write-Host "Generating self-signed STARTTLS cert for local mock SMTP..."
    & $openssl req -x509 -newkey rsa:2048 -keyout $KeyFile -out $CertFile -days 365 -nodes -subj "/CN=127.0.0.1" 2>$null
}

if (-not (Test-Path (Join-Path $BaseDir "users.yaml"))) {
    New-Item -ItemType Directory -Force -Path $BaseDir | Out-Null
    Write-Host "Creating OctoPrint admin user (admin / octotext123)..."
    & "$VenvDir\Scripts\octoprint.exe" --basedir $BaseDir user add admin --password octotext123 --admin

    @"
api:
  key: OCTOTEXT_TEST_API_KEY_12345
appearance:
  name: OctoText Test Printer
devel:
  virtualPrinter:
    enabled: true
plugins:
  OctoText:
    carrier_address: example.com
    en_webcam: true
    phone_numb: "5551234567"
    server_pass: testpass
    servername: example.com
    show_navbar_button: true
    smtp_message: "Hello from OctoText local test!"
    smtp_name: 127.0.0.1
    smtp_port: 925
    use_ssl: false
    username: octotext
  classicwebcam:
    flipH: false
    flipV: false
    rotate90: false
    snapshot: http://127.0.0.1:8089/snapshot
    stream: http://127.0.0.1:8089/snapshot
server:
  firstRun: false
  onlineCheck:
    enabled: false
  pluginBlacklist:
    enabled: false
  seenWizards:
    classicwebcam: 1
    corewizard: 4
    tracking: null
webcam:
  defaultWebcam: classic
  snapshotWebcam: classic
"@ | Set-Content -Path (Join-Path $BaseDir "config.yaml") -Encoding UTF8
}

Write-Host ""
Write-Host "Starting Mock SMTP (127.0.0.1:925) & Inbox/Webcam (http://127.0.0.1:8089)..." -ForegroundColor Cyan
$mockProc = Start-Process -FilePath "$VenvDir\Scripts\python.exe" -ArgumentList (Join-Path $TestDir "mock_services.py") -PassThru -NoNewWindow

try {
    Write-Host "Starting OctoPrint $OctoPrintVersion on http://127.0.0.1:$Port (login: admin / octotext123)..." -ForegroundColor Green
    Write-Host "Press Ctrl+C to stop both servers." -ForegroundColor Yellow
    & "$VenvDir\Scripts\octoprint.exe" --basedir $BaseDir serve --host 127.0.0.1 --port $Port
} finally {
    if ($mockProc -and -not $mockProc.HasExited) {
        Write-Host "Stopping mock services..."
        Stop-Process -Id $mockProc.Id -Force -ErrorAction SilentlyContinue
    }
}
