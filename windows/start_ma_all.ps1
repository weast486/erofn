# 이평선 매매 화면(maweb)과 실거래 봇(mabot)을 한 번에 켠다.
# 늘 마지막 버전만 켜지게: 예전 창(꺼진 뒤 '아무 키나' 대기 중인 창 포함)과 프로그램을 모두 닫고 새로 켬.
# 창은 explorer 로 열어 이 창이 닫혀도 같이 꺼지지 않게 함 (UTF-8 BOM 으로 저장)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$items = @(
    @{ Name = '이평선 매매 화면'; Pattern = 'binancebot\.maweb'; Bat = 'ma_web.bat' },
    @{ Name = '이평선 실거래 봇'; Pattern = 'binancebot\.mabot\s+run'; Bat = 'run_ma_bot.bat' }
)
$all = Get-CimInstance Win32_Process
foreach ($it in $items) {
    $old = $all | Where-Object {
        $_.CommandLine -and (
            ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $it.Pattern) -or
            ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match [regex]::Escape($it.Bat)))
    }
    foreach ($p in $old) {
        # /T = 그 창에서 켠 프로그램까지 같이 닫음
        & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null
    }
    if ($old) { Write-Host "$($it.Name): 예전 창 $(@($old).Count)개 닫음" }
}
Start-Sleep -Seconds 2
foreach ($it in $items) {
    Start-Process explorer.exe -ArgumentList ('"' + (Join-Path $here $it.Bat) + '"')
    Write-Host "$($it.Name): 새로 켰어요"
}
Write-Host ''
Write-Host '화면 주소: http://localhost:8790  (검은 창 두 개는 닫지 마세요)'
