# 바탕화면에 OKX FVG 봇 실행·드라이런 결과 바로가기를 아이콘과 함께 만든다 (UTF-8 BOM 으로 저장해야 한글 이름이 깨지지 않음)
$here = $PSScriptRoot
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$icon = (Join-Path (Join-Path $here 'icons') 'okx_bot.ico') + ',0'
foreach ($s in @(@('OKX봇 실행', 'run_okx_loop.bat'), @('OKX봇 드라이런 결과', 'okx_dryrun_report.bat'))) {
    $lnk = $shell.CreateShortcut((Join-Path $desktop ($s[0] + '.lnk')))
    $lnk.TargetPath = Join-Path $here $s[1]
    $lnk.WorkingDirectory = Split-Path -Parent $here
    $lnk.IconLocation = $icon
    $lnk.Save()
    Write-Host ('만들었습니다: ' + $s[0])
}
Write-Host '(아이콘이 바로 안 바뀌면 바탕화면에서 F5)'
