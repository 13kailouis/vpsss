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
& $cloudflared --config $config service install
Write-Host '      terpasang.'

Write-Host ''
Write-Host 'Selesai. Uji:'
Write-Host '  curl -H "x-relay-key: ISI_KUNCI" "https://ig-relay.wefluence.app/?username=ruang_ggelap"'
Write-Host ''
Write-Host 'Kalau mau dicabut lagi:'
Write-Host '  Unregister-ScheduledTask -TaskName wf-ig-relay -Confirm:$false'
Write-Host '  & "C:\Program Files (x86)\cloudflared\cloudflared.exe" service uninstall'
