# 이평선 매매 화면(maweb)과 실거래 봇(mabot)을 한 번에 켠다. 이미 켜져 있는 쪽은 다시 켜지 않음.
# 창은 explorer 로 열어 이 창이 닫혀도 같이 꺼지지 않게 함 (UTF-8 BOM 으로 저장)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | ForEach-Object { $_.CommandLine }

function Test-Running($pattern) { return [bool]($procs | Where-Object { $_ -and $_ -match $pattern }) }

$items = @(
    @{ Name = '이평선 매매 화면'; Pattern = 'binancebot\.maweb'; Bat = 'ma_web.bat' },
    @{ Name = '이평선 실거래 봇'; Pattern = 'binancebot\.mabot\s+run'; Bat = 'run_ma_bot.bat' }
)
foreach ($it in $items) {
    if (Test-Running $it.Pattern) {
        Write-Host "$($it.Name): 이미 켜져 있어요"
    } else {
        Start-Process explorer.exe -ArgumentList ('"' + (Join-Path $here $it.Bat) + '"')
        Write-Host "$($it.Name): 켰어요"
    }
}
Write-Host ''
Write-Host '화면 주소: http://localhost:8790  (검은 창 두 개는 닫지 마세요)'
