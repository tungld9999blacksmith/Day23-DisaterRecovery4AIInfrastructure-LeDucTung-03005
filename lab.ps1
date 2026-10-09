<#
  lab.ps1 -- ban PowerShell cua Makefile + scripts/up_bare.sh/down_bare.sh cho Windows.
  Venv do uv quan ly (.venv). Chaos di qua scripts/win_chaos.py (SIGSTOP/SIGKILL -> Win32).

  Dung:  .\lab.ps1 <lenh> [tham so]
  Neu bi chan ExecutionPolicy:
         powershell -ExecutionPolicy Bypass -File .\lab.ps1 <lenh>

  Lenh:
    setup            uv venv + cai requirements.txt
    seed             = make seed
    up / down        = scripts/up_bare.sh / scripts/down_bare.sh
    status           trang thai alive/ready cua 2 region
    kill    [-Region a] [-Mode netblock|stop]
    restore [-Region a]
    drill-baseline   = make drill-baseline (+ luu reports/measure-drill-1.json, restore A)
    reset            down + xoa state & log drill 2 + seed + up (GIU drill 1 + chaos-events)
    drill-dr         = make drill-dr (+ dr/runbook.py --auto nhu GUIDE Buoc 4, roi do RTO)
    rto              = make rto
    test  [args]     = make test  (vd: .\lab.ps1 test -k health_checker)
    py    <args>     chay python trong venv (vd: .\lab.ps1 py dr/failover.py --target b)
    clean            = make clean (XOA CA evidence drill 1!)
#>
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [ValidateSet("a", "b")][string]$Region = "a",
    [ValidateSet("netblock", "stop")][string]$Mode = "netblock",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"

# Report/test co tieng Viet: ep Python dung UTF-8 thay vi cp1252 cua Windows
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Ports = [ordered]@{ "region-a" = 8001; "region-b" = 8002; "edge" = 8080 }

function Assert-Venv {
    if (-not (Test-Path $Py)) { throw "Chua co .venv -- chay: .\lab.ps1 setup" }
    $p = Start-Process -FilePath $Py -ArgumentList '-c "import uvicorn, fastapi, httpx"' -NoNewWindow -Wait -PassThru `
        -RedirectStandardError (Join-Path $env:TEMP "lab23-venv-check.txt")
    if ($p.ExitCode -ne 0) { throw ".venv thieu package (uvicorn/fastapi/httpx) -- chay: .\lab.ps1 setup" }
}

function Invoke-Py {
    param([string[]]$PyArgs)
    & $Py @PyArgs
    if ($LASTEXITCODE -ne 0) { throw "python $($PyArgs -join ' ') -> exit $LASTEXITCODE" }
}

function Start-Bg {
    # Tuong duong `cmd &` trong bash: chay nen, output van ra console nay
    param([string[]]$PyArgs)
    Start-Process -FilePath $Py -ArgumentList $PyArgs -NoNewWindow -PassThru
}

function Write-Utf8 {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText((Join-Path $Root $Path), $Text, (New-Object System.Text.UTF8Encoding $false))
}

function Get-ListenPid([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess } else { return $null }
}

function Test-Url([string]$Url) {
    try { (Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200 } catch { $false }
}

function Lab-Setup {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "Chua co uv. Cai: powershell -ExecutionPolicy Bypass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
    }
    uv venv .venv --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv venv that bai" }
    uv pip install --python $Py -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "uv pip install that bai" }
    & $Py --version
}

function Lab-Seed {
    Assert-Venv
    Invoke-Py @("state/seed_vectors.py", "--region", "a", "--docs", "200")
    Invoke-Py @("state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0")
    Write-Utf8 "edge/active_region" "a"
}

function Lab-Up {
    Assert-Venv
    New-Item -ItemType Directory -Force run, reports | Out-Null
    foreach ($name in $Ports.Keys) {
        $port = $Ports[$name]
        $holder = Get-ListenPid $port
        if ($holder) { throw "Cong $port dang bi PID $holder chiem -- chay .\lab.ps1 down hoac Stop-Process -Id $holder" }
    }

    $saved = @{}
    foreach ($k in "REGION", "STATE_DIR", "WARMUP_SECONDS", "EDGE_TTL_SECONDS") { $saved[$k] = [Environment]::GetEnvironmentVariable($k) }
    $warm = if ($env:WARMUP_SECONDS) { $env:WARMUP_SECONDS } else { "6" }
    $ttl = if ($env:EDGE_TTL_SECONDS) { $env:EDGE_TTL_SECONDS } else { "5" }

    try {
        foreach ($name in $Ports.Keys) {
            $port = $Ports[$name]
            if ($name -eq "edge") {
                $env:EDGE_TTL_SECONDS = $ttl
                $module = "edge.proxy:app"
            } else {
                $r = $name.Substring($name.Length - 1)
                $env:REGION = $r; $env:STATE_DIR = "state/region-$r"; $env:WARMUP_SECONDS = $warm
                $module = "serving.app:app"
            }
            # Chay qua cmd /c + ShellExecute (khong -NoNewWindow/-Redirect*): server KHONG ke
            # thua handle console/pipe cua shell nay -> `.\lab.ps1 up | ...` khong bi treo
            $cmdLine = "/s /c `"`"$Py`" -m uvicorn $module --host 127.0.0.1 --port $port --log-level warning > `"run\$name.log`" 2>&1`""
            $p = Start-Process -FilePath "cmd.exe" -ArgumentList $cmdLine -WindowStyle Hidden -PassThru
            # .venv\Scripts\python.exe la launcher -> PID that cua server lay sau, theo cong
            Write-Utf8 "run\$name.launcher.pid" "$($p.Id)"
        }
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    }

    # PID song KHONG co nghia la da bind xong cong -> verify bang HTTP that (giong up_bare.sh)
    Write-Host "cho service len (toi da 15s)..."
    $ok = $true
    foreach ($name in $Ports.Keys) {
        $port = $Ports[$name]
        $path = if ($name -eq "edge") { "edge/state" } else { "healthz" }
        $up = $false
        for ($i = 0; $i -lt 15; $i++) {
            if (Test-Url "http://127.0.0.1:$port/$path") { $up = $true; break }
            Start-Sleep -Seconds 1
        }
        $listenPid = Get-ListenPid $port
        if ($up -and $listenPid) {
            # chaos/kill_region.py doc file nay -> phai la PID cua process dang LISTEN
            Write-Utf8 "run\$name.pid" "$listenPid"
            Write-Host "  $name (port $port): UP  pid=$listenPid"
        } else {
            Write-Host "  $name (port $port): KHONG PHAN HOI -- xem run\$name.log" -ForegroundColor Red
            $ok = $false
        }
    }
    if (-not $ok) { throw "MOT SO SERVICE CHUA LEN -- doc log truoc khi chay drill" }
    (Invoke-WebRequest -Uri "http://127.0.0.1:8080/edge/state" -UseBasicParsing).Content
}

function Lab-Down {
    $pids = @()
    foreach ($f in Get-ChildItem run -Filter *.pid -ErrorAction SilentlyContinue) {
        $v = (Get-Content $f.FullName -Raw -ErrorAction SilentlyContinue)
        if ($v -and $v.Trim()) { $pids += [int]$v.Trim() }
        Remove-Item $f.FullName -Force
    }
    foreach ($port in $Ports.Values) { $l = Get-ListenPid $port; if ($l) { $pids += $l } }
    foreach ($id in ($pids | Sort-Object -Unique)) {
        # TerminateProcess giet duoc ca process dang bi suspend (netblock)
        try { Stop-Process -Id $id -Force -ErrorAction Stop } catch {}
    }
    Write-Host "all stopped"
}

function Lab-Chaos([string[]]$ChaosArgs) {
    Assert-Venv
    Invoke-Py (@("scripts/win_chaos.py") + $ChaosArgs)
}

function Lab-DrillBaseline {
    Assert-Venv
    $lg = Start-Bg @("loadgen/traffic.py", "--duration", "40", "--rps", "2", "--out", "reports/drill-1-nodr.jsonl")
    Start-Sleep -Seconds 8
    Lab-Chaos @("--region", "a", "--mode", "netblock", "--mock")
    Write-Host "doi loadgen chay het 40s..."
    $lg.WaitForExit()
    $out = & $Py tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
    Write-Utf8 "reports/measure-drill-1.json" (($out -join "`n") + "`n")
    $out
    Write-Host "-> da luu reports/measure-drill-1.json. Restore region A:"
    Lab-Chaos @("restore", "--region", "a", "--backend", "bare")
}

function Lab-Reset {
    Lab-Down
    foreach ($d in "state/region-a", "state/region-b", "state/_replica") {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    }
    foreach ($f in "drill-2-withdr.jsonl", "health-events.jsonl", "failover-events.jsonl", "replication.jsonl", "runbook-run.jsonl") {
        Remove-Item "reports/$f" -Force -ErrorAction SilentlyContinue
    }
    Lab-Seed
    Lab-Up
}

function Lab-DrillDr {
    Assert-Venv
    # replicate.py phai co it nhat 1 chu ky put TRUOC, khong thi failover chet o 2_restore_snapshot
    $bg = @()
    $bg += Start-Bg @("state/ingest.py", "--region", "a", "--rate", "0.5", "--duration", "150")
    $bg += Start-Bg @("state/replicate.py", "--every", "30", "--duration", "150", "--backend", "fs")
    Start-Sleep -Seconds 5
    $bg += Start-Bg @("loadgen/traffic.py", "--duration", "100", "--rps", "2", "--out", "reports/drill-2-withdr.jsonl")
    $bg += Start-Bg @("dr/health_checker.py", "--interval", "5", "--threshold", "3", "--duration", "100", "--out", "reports/health-events.jsonl")
    Start-Sleep -Seconds 12
    Lab-Chaos @("--region", "a", "--mode", "netblock", "--mock")
    & $Py dr/runbook.py --primary a --target b --backend fs --auto
    if ($LASTEXITCODE -ne 0) { Write-Host "runbook.py exit $LASTEXITCODE" -ForegroundColor Red }
    Write-Host "doi loadgen/health checker/ingest/replicate chay xong (~150s tu luc bat dau)..."
    $bg | ForEach-Object { $_.WaitForExit() }
    Lab-Rto
}

function Lab-Rto {
    Assert-Venv
    & $Py tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
}

function Lab-Clean {
    Lab-Down
    foreach ($d in "state/region-a", "state/region-b", "state/_replica") {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    }
    Get-ChildItem run -Exclude .gitkeep -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
    Remove-Item reports/*.jsonl, reports/*.json, chaos/chaos-events.jsonl -Force -ErrorAction SilentlyContinue
    Write-Host "cleaned"
}

switch ($Command) {
    "setup"          { Lab-Setup }
    "seed"           { Lab-Seed }
    "up"             { Lab-Up }
    "down"           { Lab-Down }
    "status"         { Lab-Chaos @("status") }
    "kill"           { Lab-Chaos @("--region", $Region, "--mode", $Mode, "--mock") }
    "restore"        { Lab-Chaos @("restore", "--region", $Region, "--backend", "bare") }
    "drill-baseline" { Lab-DrillBaseline }
    "reset"          { Lab-Reset }
    "drill-dr"       { Lab-DrillDr }
    "rto"            { Lab-Rto }
    "test"           { Assert-Venv; & $Py -m pytest tests/ -v @Rest }
    "py"             { Assert-Venv; & $Py @Rest }
    "clean"          { Lab-Clean }
    default          { Get-Help $PSCommandPath; (Get-Content $PSCommandPath -TotalCount 26) -join "`n" }
}
