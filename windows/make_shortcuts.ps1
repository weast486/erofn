# 바탕화면에 토스봇 바로가기 3개를 만든다 (UTF-8 BOM 으로 저장해야 한글 이름이 깨지지 않음)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$items = @(
    @('토스봇 실행', 'run_bot.bat'),
    @('토스봇 상태확인', 'status.bat'),
    @('토스봇 설정확인', 'config.bat')
)
foreach ($it in $items) {
    $lnk = $shell.CreateShortcut((Join-Path $desktop ($it[0] + '.lnk')))
    $lnk.TargetPath = Join-Path $here $it[1]
    $lnk.WorkingDirectory = Split-Path -Parent $here
    $lnk.IconLocation = (Join-Path $here 'toss_bot.ico') + ',0'
    $lnk.Save()
    Write-Host ('만들었습니다: ' + $it[0])
}
