# 바탕화면에 바이낸스 봇 실행 바로가기를 아이콘과 함께 만든다 (UTF-8 BOM 으로 저장해야 한글 이름이 깨지지 않음)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
# 예전에 만든 연결확인·기록 바로가기는 지움
foreach ($old in @('바이낸스봇 연결확인', '바이낸스봇 기록')) {
    $p = Join-Path $desktop ($old + '.lnk')
    if (Test-Path $p) { Remove-Item $p; Write-Host ('지웠습니다: ' + $old) }
}
$lnk = $shell.CreateShortcut((Join-Path $desktop '바이낸스봇 실행.lnk'))
$lnk.TargetPath = Join-Path $here 'run_binance_loop.bat'
$lnk.WorkingDirectory = Split-Path -Parent $here
$lnk.IconLocation = (Join-Path (Join-Path $here 'icons') 'binance_bot.ico') + ',0'
$lnk.Save()
Write-Host '만들었습니다: 바이낸스봇 실행 (아이콘이 바로 안 바뀌면 바탕화면에서 F5)'
