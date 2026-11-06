# INTENT-win one-line installer
#
#   irm https://raw.githubusercontent.com/li-renji/intent-win/main/install.ps1 | iex
#
# ASCII-only on purpose: PowerShell 5.1 / irm|iex must not depend on code page.
# No "exit" anywhere: this script may run inside an interactive shell.
#
# Optional environment overrides (set BEFORE the one-liner):
#   INTENT_INSTALL_DIR    where to install (default D:\INTENT-win, or %LOCALAPPDATA%\INTENT-win)
#   INTENT_SOURCE_ZIP     use a local source zip instead of downloading (offline install)
#   INTENT_SKIP_RUNTIME=1 reuse whatever is already in <dir>\runtime, download nothing
#   INTENT_NO_PATH=1      do not touch user PATH
#   INTENT_CHECK_SOURCES=1 only probe download URLs, install nothing

& {
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo       = 'li-renji/intent-win'
$Branch     = 'main'
$PyVer      = '3.13.14'
$ChromeVer  = '158.0.8127.18'
$SourceUrls = @(
    "https://codeload.github.com/$Repo/zip/refs/heads/$Branch",
    "https://api.github.com/repos/$Repo/zipball/$Branch"
)
$PyUrls = @(
    "https://mirrors.huaweicloud.com/python/$PyVer/python-$PyVer-amd64.exe",
    "https://www.python.org/ftp/python/$PyVer/python-$PyVer-amd64.exe"
)
$ChromeUrl = "https://cdn.npmmirror.com/binaries/chrome-for-testing/$ChromeVer/win64/chrome-win64.zip"
$Wv2Url    = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
$PipMirror = "https://pypi.tuna.tsinghua.edu.cn/simple"

function Say($m)  { Write-Host "[INTENT] $m" }
function Warn($m) { Write-Host "[INTENT][WARN] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[INTENT][ERROR] $m" -ForegroundColor Red; throw $m }

$curlCmd = Get-Command 'curl.exe' -ErrorAction SilentlyContinue

function Test-Source($u) {
    try {
        if ($curlCmd) {
            $c = & $curlCmd.Source -sIL --max-time 15 -o NUL -w '%{http_code}' $u 2>$null
            return ($c -match '^[23]')
        }
        $r = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 15
        return ($r.StatusCode -lt 400)
    } catch { return $false }
}

function Get-File($urls, $out) {
    foreach ($u in @($urls)) {
        Say "downloading $u"
        try {
            if ($curlCmd) {
                & $curlCmd.Source -sSL --fail --max-time 900 --retry 2 -o $out $u
            } else {
                Invoke-WebRequest -Uri $u -OutFile $out -UseBasicParsing -TimeoutSec 900
            }
            if ((Test-Path -LiteralPath $out) -and ((Get-Item -LiteralPath $out).Length -gt 1024)) {
                return
            }
        } catch { Warn "source failed: $u" }
    }
    Die "could not download: $out"
}

# --------------------------------------------------------------- sources only
if ($env:INTENT_CHECK_SOURCES -eq '1') {
    Say 'checking download sources (no install)'
    $all = @($SourceUrls) + @($PyUrls) + @($ChromeUrl, $Wv2Url, $PipMirror)
    $bad = 0
    foreach ($u in $all) {
        $ok = Test-Source $u
        if (-not $ok) { $bad++ }
        Say ("  {0}  {1}" -f $(if ($ok) { 'OK  ' } else { 'FAIL' }), $u)
    }
    Say "sources: $(@($all).Count - $bad)/$(@($all).Count) reachable"
    return
}

# --------------------------------------------------------------- target dir
$target = $env:INTENT_INSTALL_DIR
if (-not $target) {
    if (Test-Path 'D:\') { $target = 'D:\INTENT-win' }
    else { $target = Join-Path $env:LOCALAPPDATA 'INTENT-win' }
}
$skipRuntime = ($env:INTENT_SKIP_RUNTIME -eq '1')
$tmp = Join-Path $env:TEMP ('intent-install-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
New-Item -ItemType Directory -Force -Path $target | Out-Null
Say "install dir: $target"
if ($skipRuntime) { Say 'INTENT_SKIP_RUNTIME=1 : runtime download steps skipped' }

try {
    # ----------------------------------------------------------- 1 source zip
    $zip = Join-Path $tmp 'source.zip'
    if ($env:INTENT_SOURCE_ZIP -and (Test-Path -LiteralPath $env:INTENT_SOURCE_ZIP)) {
        Copy-Item -LiteralPath $env:INTENT_SOURCE_ZIP -Destination $zip
        Say "using local source archive: $($env:INTENT_SOURCE_ZIP)"
    } else {
        Get-File $SourceUrls $zip
    }

    # ----------------------------------------------------------- 2 python
    $pyDir = Join-Path $target 'runtime\python'
    $pyExe = Join-Path $pyDir 'python.exe'
    if (-not (Test-Path -LiteralPath $pyExe) -and -not $skipRuntime) {
        $inst = Join-Path $tmp 'python-installer.exe'
        Get-File $PyUrls $inst
        Say 'installing private Python (no admin rights, no system changes)'
        $pyArgs = @('/quiet', 'InstallAllUsers=0', "TargetDir=$pyDir",
                    'Include_launcher=0', 'Include_test=0', 'Include_doc=0',
                    'Include_pip=1', 'AssociateFiles=0', 'Shortcuts=0', 'PrependPath=0')
        $p = Start-Process -FilePath $inst -ArgumentList $pyArgs -Wait -PassThru
        if (-not (Test-Path -LiteralPath $pyExe)) {
            Die "python installer failed (exit $($p.ExitCode))"
        }
    }

    # ----------------------------------------------------------- 3 unpack source
    $unpacker = Join-Path $tmp 'unpack.py'
    @'
import os, shutil, sys, zipfile
z, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
with zipfile.ZipFile(z) as f:
    for n in f.namelist():
        if n.endswith("/"):
            continue
        rel = n.split("/", 1)[1] if "/" in n else n
        if not rel:
            continue
        # never overwrite local-only data
        if os.path.exists(os.path.join(dst, rel)) and (
                rel.startswith("runtime/") or rel.startswith("logs/") or
                rel.startswith("backups/") or
                (rel.endswith(".qdai") and not rel.endswith(".example.qdai"))):
            continue
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with f.open(n) as src, open(out, "wb") as w:
            shutil.copyfileobj(src, w)
print("unpacked")
'@ | Set-Content -LiteralPath $unpacker -Encoding ASCII

    if (Test-Path -LiteralPath $pyExe) {
        & $pyExe $unpacker $zip $target | Out-Null
        if ($LASTEXITCODE -ne 0) { Die 'source unpack failed' }
    } else {
        Warn 'no python yet: falling back to tar.exe / Expand-Archive'
        $tarCmd = Get-Command 'tar.exe' -ErrorAction SilentlyContinue
        if ($tarCmd) {
            & $tarCmd.Source -xf $zip -C $tmp
            $inner = @(Get-ChildItem -LiteralPath $tmp -Directory |
                       Where-Object { $_.Name -like '*intent-win*' } | Select-Object -First 1)
            if ($inner.Count -eq 0) { Die 'tar unpack produced no folder' }
            & robocopy $inner[0].FullName $target /E /NFL /NDL /NJH /NJS /nc /ns /np /R:1 /W:1 | Out-Null
            if ($LASTEXITCODE -ge 8) { Die 'copy failed' }
        } else {
            $ex = Join-Path $tmp 'src'
            Expand-Archive -LiteralPath $zip -DestinationPath $ex -Force
            $inner = @(Get-ChildItem -LiteralPath $ex -Directory | Select-Object -First 1)
            & robocopy $inner[0].FullName $target /E /NFL /NDL /NJH /NJS /nc /ns /np /R:1 /W:1 | Out-Null
            if ($LASTEXITCODE -ge 8) { Die 'copy failed' }
        }
    }
    Say 'source files in place'

    if (-not $skipRuntime) {
        # ------------------------------------------------------- 4 pip deps
        $req = Join-Path $target 'requirements.txt'
        if ((Test-Path -LiteralPath $req) -and (Test-Path -LiteralPath $pyExe)) {
            Say 'installing python dependencies (private runtime only)'
            $saved = @{}
            foreach ($n in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY',
                             'http_proxy','https_proxy','all_proxy')) {
                $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
                [Environment]::SetEnvironmentVariable($n, $null, 'Process')
            }
            try {
                & $pyExe -m pip install --disable-pip-version-check --no-warn-script-location `
                    -i $PipMirror -r $req 2>&1 | Select-Object -Last 6 | Write-Host
            } finally {
                foreach ($n in $saved.Keys) {
                    [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process')
                }
            }
        }

        # ------------------------------------------------------- 5 WebView2
        $wv2Dir = Join-Path $target 'runtime\webview2'
        $wv2Exe = Join-Path $wv2Dir 'msedgewebview2.exe'
        if (-not (Test-Path -LiteralPath $wv2Exe)) {
            $sysRoot = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\EdgeWebView\Application'
            $ver = $null
            if (Test-Path -LiteralPath $sysRoot) {
                $ver = Get-ChildItem -LiteralPath $sysRoot -Directory |
                       Where-Object { $_.Name -match '^\d+\.' } |
                       Sort-Object Name -Descending | Select-Object -First 1
            }
            if (-not $ver) {
                Say 'installing WebView2 runtime (bootstrapper)'
                $boot = Join-Path $tmp 'webview2setup.exe'
                Get-File $Wv2Url $boot
                $p = Start-Process -FilePath $boot -ArgumentList @('/silent','/install') -Wait -PassThru
                if (Test-Path -LiteralPath $sysRoot) {
                    $ver = Get-ChildItem -LiteralPath $sysRoot -Directory |
                           Where-Object { $_.Name -match '^\d+\.' } |
                           Sort-Object Name -Descending | Select-Object -First 1
                }
            }
            if ($ver) {
                Say 'bundling WebView2 runtime into the app folder'
                & robocopy $ver.FullName $wv2Dir /E /NFL /NDL /NJH /NJS /nc /ns /np /R:1 /W:1 | Out-Null
                if ($LASTEXITCODE -ge 8) { Warn 'WebView2 copy incomplete' }
            } else {
                Warn 'WebView2 runtime unavailable: main window will fall back to the system one'
            }
        }

        # ------------------------------------------------------- 6 Chromium
        $chromeExe = Join-Path $target 'runtime\chromium\chrome.exe'
        if (-not (Test-Path -LiteralPath $chromeExe)) {
            $czip = Join-Path $tmp 'chrome.zip'
            Get-File $ChromeUrl $czip
            Say 'unpacking Chromium'
            $unpackChrome = Join-Path $tmp 'unpack_chrome.py'
            @'
import os, shutil, sys, zipfile
z, tmp, dst = sys.argv[1:4]
if not os.path.isfile(os.path.join(dst, "chrome.exe")):
    with zipfile.ZipFile(z) as f:
        f.extractall(tmp)
    src = os.path.join(tmp, "chrome-win64")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.move(src, dst)
print("chromium ok")
'@ | Set-Content -LiteralPath $unpackChrome -Encoding ASCII
            & $pyExe $unpackChrome $czip $tmp (Join-Path $target 'runtime\chromium') | Out-Null
            if ($LASTEXITCODE -ne 0) { Die 'Chromium unpack failed' }
        }
    }

    # ----------------------------------------------------------- 7 config
    $example = Get-ChildItem -LiteralPath $target -Filter '*.example.qdai' -File |
               Select-Object -First 1
    if ($example) {
        $cfgName = ($example.BaseName -replace '\.example$', '') + '.qdai'
        $cfg = Join-Path $target $cfgName
        if (-not (Test-Path -LiteralPath $cfg)) {
            Copy-Item -LiteralPath $example.FullName -Destination $cfg
            Say 'created local config (fill in your own api_key before first chat)'
        } else {
            Say 'existing local config preserved'
        }
    }

    # ----------------------------------------------------------- 8 PATH
    if ($env:INTENT_NO_PATH -ne '1') {
        $logDir = Join-Path $target 'logs'
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $backup = Join-Path $logDir ('user_path_backup_' + (Get-Date -Format yyyyMMdd) + '.txt')
        if (-not (Test-Path -LiteralPath $backup)) {
            Set-Content -LiteralPath $backup -Value $userPath -Encoding UTF8 -NoNewline
        }
        $present = @($userPath -split ';' |
                     Where-Object { $_.TrimEnd('\') -ieq $target.TrimEnd('\') }).Count -gt 0
        if (-not $present) {
            [Environment]::SetEnvironmentVariable('Path',
                ($userPath.TrimEnd(';') + ';' + $target), 'User')
            Say 'added install dir to user PATH (backup saved in logs/)'
        }
        if ($env:Path -notlike "*$target*") {
            $env:Path = $env:Path.TrimEnd(';') + ';' + $target
        }
    } else {
        Say 'INTENT_NO_PATH=1 : PATH untouched'
    }

    # ----------------------------------------------------------- 9 self check
    $chat = Join-Path $target 'chat.py'
    if ((Test-Path -LiteralPath $pyExe) -and (Test-Path -LiteralPath $chat)) {
        Say 'running self check (intent --runtime)'
        & $pyExe $chat '--runtime'
    }

    Say "install complete: $target"
    Say 'open a NEW terminal and type:  intent'
    Say 'GUI: double-click start.bat'
    Say 'check bundling anytime:  intent --runtime'
} finally {
    try { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue } catch {}
}
}
