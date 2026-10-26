# 바탕화면에 바이낸스 SOXL 스플릿 봇 실행 바로가기를 아이콘과 함께 만든다 (UTF-8 BOM 으로 저장해야 한글 이름이 깨지지 않음)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop '바이낸스 스플릿 봇 (켜두기).lnk'))
$lnk.TargetPath = Join-Path $here 'run_binance_split.bat'
$lnk.WorkingDirectory = Split-Path -Parent $here
$lnk.IconLocation = (Join-Path $here 'split_bot.ico') + ',0'
$lnk.Save()
Write-Host '만들었습니다: 바이낸스 스플릿 봇 (켜두기) (아이콘이 바로 안 바뀌면 바탕화면에서 F5)'