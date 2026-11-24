# OctoText Local Test Environment

Reusable local test harness for testing OctoText PRs and releases against any OctoPrint version (e.g. `2.0.1`, `1.10.3`) without needing a physical printer, webcam, or real SMTP credentials.

## Prerequisites
- `uv` installed (`uv --version`)
- Git for Windows (provides `openssl.exe` for the local self-signed STARTTLS cert)

## Quick Start

### 1. Run the live local OctoPrint + Mock Inbox/Webcam environment
```powershell
# Default: OctoPrint 2.0.1 on Python 3.12
.\local-test\start-test-env.ps1

# Or test against OctoPrint 1.10.3 on Python 3.11
.\local-test\start-test-env.ps1 -OctoPrintVersion 1.10.3 -PythonVersion 3.11
```

Then open:
- **OctoPrint:** http://127.0.0.1:5000 (`admin` / `octotext123`)
- **Local Email Inbox & Mock Webcam:** http://127.0.0.1:8089

Press `Ctrl+C` in the terminal to stop both servers cleanly.

### 2. Run the automated pytest suite
```powershell
# Against OctoPrint 2.0.1
.\local-test\run-tests.ps1

# Against OctoPrint 1.10.3
.\local-test\run-tests.ps1 -OctoPrintVersion 1.10.3 -PythonVersion 3.11
```

### 3. Build & verify wheel/sdist packaging
```powershell
uv build --out-dir .\local-test\.dist .
```

## Notes
- Generated virtualenvs (`.venv-*`), OctoPrint data (`.basedir-*`), certs (`smtp.crt`/`smtp.key`), and build artifacts (`.dist`) live inside `local-test/`.
- If you want to keep `local-test/` out of `git status` without modifying `.gitignore`, add `local-test/` to `.git/info/exclude`.
