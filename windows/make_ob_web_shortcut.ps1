# 바탕화면에 오더블록 화면 바로가기를 아이콘과 함께 만든다 (UTF-8 BOM 으로 저장해야 한글 이름이 깨지지 않음)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop '오더블록 화면.lnk'))
$lnk.TargetPath = Join-Path $here 'ob_web.bat'
$lnk.WorkingDirectory = Split-Path -Parent $here
$lnk.IconLocation = (Join-Path $here 'icons\ob_web.ico') + ',0'
$lnk.Description = '바이낸스 장악형 오더블록·겹치는 자리 화면 (주문 없음)'
$lnk.Save()
Write-Host '만들었습니다: 오더블록 화면 (아이콘이 바로 안 바뀌면 바탕화면에서 F5)'
