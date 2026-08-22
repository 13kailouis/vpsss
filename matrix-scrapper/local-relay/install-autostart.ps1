# Pasang relay + tunnel supaya nyala sendiri setiap mesin dinyalakan.
#
# Jalankan SEKALI dari PowerShell "Run as Administrator":
#     .\install-autostart.ps1
#
# Dua hal yang dipasang:
#   1. Tugas terjadwal untuk relay.py, dijalankan saat mesin menyala, tanpa
#      jendela. Kuncinya dibaca dari relay.key, bukan ditulis di perintah tugas,
#      supaya tidak ikut terbaca di daftar Task Scheduler.
#   2. cloudflared sebagai Windows service, memakai config.yml di folder ini.
#
# Tanpa ini, relay dan tunnel mati begitu mesin restart, dan panggilan grid dari
# VPS ikut gagal.

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$relay = Join-Path $here 'relay.py'
$keyFile = Join-Path $here 'relay.key'
$config = Join-Path $here 'config.yml'
$pythonw = 'C:\ProgramData\miniconda3\pythonw.exe'
$cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'

# Diperiksa dulu satu per satu. Memasang tugas yang menunjuk ke berkas yang
# tidak ada akan "berhasil" dipasang lalu gagal diam-diam tiap kali boot.
foreach ($p in @($relay, $keyFile, $config, $pythonw, $cloudflared)) {
    if (-not (Test-Path $p)) { throw "Tidak ketemu: $p" }
}

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Harus dijalankan dari PowerShell Run as Administrator.'
}

Write-Host '[1/2] Memasang tugas terjadwal untuk relay...'
$taskName = 'wf-ig-relay'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$relay`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host '      terpasang dan dijalankan.'

Write-Host '[2/2] Memasang cloudflared sebagai service...'

# Service berjalan sebagai SYSTEM, dan SYSTEM tidak melihat isi
# C:\Users\<kamu>\.cloudflared. Menjalankan `service install` sambil menunjuk
# config di folder pengguna menghasilkan service yang terpasang dan berjalan,
# tapi PathName-nya kosong dari argumen sehingga tidak menyambungkan apa pun --
# gejalanya Cloudflare membalas 530 padahal service statusnya Running.
# Jadi config dan kredensial disalin dulu ke profil SYSTEM.
$sysDir = 'C:\Windows\System32\config\systemprofile\.cloudflared'
New-Item -ItemType Directory -Force -Path $sysDir | Out-Null

$cfg = Get-Content $config -Raw
$credLine = [regex]::Match($cfg, '(?m)^\s*credentials-file:\s*(.+?)\s*$')
if (-not $credLine.Success) { throw "config.yml tidak punya baris credentials-file" }
$credSrc = $credLine.Groups[1].Value.Trim()
if (-not (Test-Path $credSrc)) { throw "Berkas kredensial tidak ketemu: $credSrc" }

$credDst = Join-Path $sysDir (Split-Path $credSrc -Leaf)
Copy-Item $credSrc $credDst -Force

# Config yang disalin harus menunjuk ke kredensial yang ikut disalin, bukan ke
# lokasi lama di folder pengguna yang tidak terjangkau SYSTEM.
$cfgSys = $cfg -replace [regex]::Escape($credSrc), $credDst.Replace('\', '\\')
Set-Content -Path (Join-Path $sysDir 'config.yml') -Value $cfgSys -Encoding utf8

$svc = Get-Service Cloudflared -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host '      service lama dicabut dulu...'
    & $cloudflared service uninstall | Out-Null
    Start-Sleep -Seconds 2
}
& $cloudflared service install
Start-Sleep -Seconds 3
Start-Service Cloudflared -ErrorAction SilentlyContinue
Write-Host '      terpasang.'

$path = (Get-CimInstance Win32_Service -Filter "Name='Cloudflared'").PathName
Write-Host "      PathName: $path"

Write-Host ''
Write-Host 'Selesai. Uji:'
Write-Host '  curl -H "x-relay-key: ISI_KUNCI" "https://ig-relay.wefluence.app/?username=ruang_ggelap"'
Write-Host ''
Write-Host 'Kalau mau dicabut lagi:'
Write-Host '  Unregister-ScheduledTask -TaskName wf-ig-relay -Confirm:$false'
Write-Host '  & "C:\Program Files (x86)\cloudflared\cloudflared.exe" service uninstall'
