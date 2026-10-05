# 바탕화면에 바이낸스 봇 바로가기 3개를 아이콘과 함께 만든다 (UTF-8 BOM 으로 저장해야 한글 이름이 깨지지 않음)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$items = @(
    @('바이낸스봇 실행', 'run_binance_loop.bat', 'binance_run.ico'),
    @('바이낸스봇 연결확인', 'binance_check.bat', 'binance_check.ico'),
    @('바이낸스봇 기록', 'binance_log.bat', 'binance_log.ico')
)
foreach ($it in $items) {
    $lnk = $shell.CreateShortcut((Join-Path $desktop ($it[0] + '.lnk')))
    $lnk.TargetPath = Join-Path $here $it[1]
    $lnk.WorkingDirectory = Split-Path -Parent $here
    $lnk.IconLocation = (Join-Path (Join-Path $here 'icons') $it[2]) + ',0'
    $lnk.Save()
    Write-Host ('만들었습니다: ' + $it[0])
}
Write-Host '바탕화면에 바로가기 3개가 생겼어요. 아이콘이 바로 안 바뀌면 바탕화면에서 F5(새로고침)를 눌러 주세요.'
