# 바탕화면에 '이평선 매매 (화면+봇)' 바로가기 하나를 만든다 — 누르면 화면과 실거래 봇을 같이 켬 (UTF-8 BOM 으로 저장해야 한글 이름이 깨지지 않음)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell

# 예전에 따로 만들던 바로가기 두 개는 지움
foreach ($old in @('이평선 매매 화면.lnk', '이평선 매매 봇.lnk')) {
    $p = Join-Path $desktop $old
    if (Test-Path $p) { Remove-Item $p }
}

$lnk = $shell.CreateShortcut((Join-Path $desktop '이평선 매매 (화면+봇).lnk'))
$lnk.TargetPath = Join-Path $here 'start_ma_all.bat'
$lnk.WorkingDirectory = Split-Path -Parent $here
$lnk.IconLocation = (Join-Path $here 'icons\ma_web.ico') + ',0'
$lnk.Description = '이평선 매매 화면(http://localhost:8790)과 실거래 봇을 같이 켬, 이미 켜진 쪽은 그대로'
$lnk.Save()

Write-Host '만들었습니다: 이평선 매매 (화면+봇) (아이콘이 바로 안 바뀌면 바탕화면에서 F5)'
