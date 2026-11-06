param([string]$Archive, [string]$PackageDir, [switch]$Child)
$ErrorActionPreference = 'Stop'

if ($Child) {
    $env:HTTP_PROXY = $env:HTTPS_PROXY = $env:ALL_PROXY = 'http://127.0.0.1:9'
    $env:NO_PROXY = '127.0.0.1,localhost,::1'
    $env:HF_HUB_OFFLINE = $env:TRANSFORMERS_OFFLINE = '1'
    Set-Location $PackageDir
    & "$PackageDir/python/python.exe" "$PackageDir/offline_smoke.py" $PackageDir
    exit $LASTEXITCODE
}

$root = Join-Path $env:RUNNER_TEMP 'pfc-offline-package'
$evidence = Join-Path $env:RUNNER_TEMP 'pfc-offline-evidence'
New-Item -ItemType Directory -Force -Path $root, $evidence | Out-Null
Expand-Archive -Path $Archive -DestinationPath $root
Copy-Item "$PSScriptRoot/offline_smoke.py", "$PSScriptRoot/offline_smoke.ps1", "$PSScriptRoot/MODELS.lock" $root
$user = 'pfc-offline-smoke'
$password = ConvertTo-SecureString ('Pfc!' + [guid]::NewGuid().ToString('N')) -AsPlainText -Force
$account = New-LocalUser -Name $user -Password $password -AccountNeverExpires
$users = Get-LocalGroup -SID 'S-1-5-32-545'
if (-not (Get-LocalGroupMember $users | Where-Object SID -eq $account.SID)) {
    Add-LocalGroupMember -Group $users -Member $user
}
& icacls.exe $root /grant "${user}:(OI)(CI)M" /T /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not grant package access to the standard user' }
$rules = @()
$process = $null
$violations = @()
$profiles = Get-NetFirewallProfile | Select-Object Name, Enabled
try {
    Set-NetFirewallProfile -Profile Domain,Private,Public -Enabled True
    # Scope isolation to every network-capable app executable, not the Actions agent.
    $remote = @('0.0.0.0-126.255.255.255', '128.0.0.0-255.255.255.255', '::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff')
    foreach ($exe in @((Join-Path $root 'python\python.exe'), (Join-Path $root 'llama\llama-server.exe'))) {
        $name = 'PFC offline ' + [guid]::NewGuid().ToString('N')
        New-NetFirewallRule -DisplayName $name -Direction Outbound -Action Block -Enabled True -Profile Any -Program $exe -RemoteAddress $remote | Out-Null
        $rules += $name
    }
    $credential = [pscredential]::new("$env:COMPUTERNAME\$user", $password)
    $arguments = "-NoProfile -File `"$root/offline_smoke.ps1`" -Child -PackageDir `"$root`""
    $process = Start-Process -FilePath (Get-Command pwsh).Source -ArgumentList $arguments -Credential $credential -LoadUserProfile -PassThru -RedirectStandardOutput "$root/stdout.txt" -RedirectStandardError "$root/stderr.txt"
    $deadline = (Get-Date).AddMinutes(15)
    while (-not $process.HasExited) {
        if ((Get-Date) -gt $deadline) { throw 'Offline smoke timed out' }
        $ids = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$root*" } | Select-Object -ExpandProperty Id)
        $connections = Get-NetTCPConnection -State Established,SynSent -ErrorAction SilentlyContinue
        foreach ($connection in $connections) {
            if ($connection.OwningProcess -in $ids -and -not [System.Net.IPAddress]::IsLoopback([ipaddress]$connection.RemoteAddress)) {
                $violations += "$($connection.OwningProcess) $($connection.RemoteAddress):$($connection.RemotePort)"
            }
        }
        Start-Sleep -Milliseconds 250
        $process.Refresh()
    }
    $process.WaitForExit()
    Get-Content "$root/stdout.txt", "$root/stderr.txt"
    if ($process.ExitCode -ne 0) { throw "Standard-user smoke exited $($process.ExitCode)" }
    if ($violations.Count) { throw "Non-loopback app connections observed: $($violations -join ', ')" }
    $metrics = Get-Content "$root/smoke-metrics.json" -Raw | ConvertFrom-Json
    if (-not $metrics.shutdown_clean -or $metrics.token_elevated -or $metrics.administrator) { throw 'Smoke evidence failed' }
    Copy-Item "$root/smoke-metrics.json" $evidence
    @"
## Model-complete offline first run — Windows CI/VM, not target laptop
- ZIP size: $((Get-Item $Archive).Length) bytes
- Launcher startup to healthy HTTP: $($metrics.startup_seconds) s
- Models exercised (generation + re-embed): $($metrics.models_ready_seconds) s
- Peak RSS (100 ms sampled aggregate launcher + llama-server): $($metrics.peak_rss_bytes) bytes
- Non-admin: dedicated local Users-group account; actual launcher TokenElevation=false and IsUserAnAdmin=false
- Egress: outbound firewall blocks non-loopback IPv4/IPv6 for packaged Python and llama-server; dead HTTP(S)/ALL proxies
- Non-loopback app TCP connections observed: $($violations.Count) (snapshot sampling, not proof of zero attempted packets)
- Re-embed: $($metrics.reembed_job_status); generation: $($metrics.generation_chars) characters; clean shutdown: $($metrics.shutdown_clean)
- CI explicitly exercises the pinned GGUF; production model approval defaults are unchanged.
"@ | Add-Content $env:GITHUB_STEP_SUMMARY
} finally {
    if ($process -and -not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
    }
    foreach ($rule in $rules) { Remove-NetFirewallRule -DisplayName $rule -ErrorAction Continue }
    foreach ($profile in $profiles) { Set-NetFirewallProfile -Profile $profile.Name -Enabled $profile.Enabled -ErrorAction Continue }
    Copy-Item "$root/stderr.txt" $evidence -ErrorAction SilentlyContinue
    Remove-LocalUser -Name $user -ErrorAction Continue
}
