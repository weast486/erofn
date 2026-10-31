# 같은 bat 으로 열었다가 봇이 끝나 'pause' 에서 멈춰 있는 예전 창을 닫는다 (2026-10-31 사용자 요청).
# 봇(python)이 아직 돌고 있는 창은 건드리지 않는다. 사용: powershell -File close_old_windows.ps1 run_binance_split.bat
param([string]$Bat)
$me = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
    Where-Object { $_.CommandLine -like "*$Bat*" -and $_.ProcessId -ne $me } |
    Where-Object { -not (Get-CimInstance Win32_Process -Filter "ParentProcessId=$($_.ProcessId) AND Name='python.exe'") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
