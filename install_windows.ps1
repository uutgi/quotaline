# Windows installer for quotaline. Asks three questions (or takes them as parameters):
#   -Show both|claude|openrouter     what to display
#   -Launcher window|widget|tray|none  window = small terminal, widget = floating panel (no extra
#                                      packages), tray = notification-area icon (installs pystray)
#   -Autostart yes|no                start the launcher with Windows
# Also configures the Claude Code status line when `claude` is installed.
#   powershell -ExecutionPolicy Bypass -File install_windows.ps1
#   powershell -ExecutionPolicy Bypass -File install_windows.ps1 -Show both -Launcher widget -Autostart no
param(
    [ValidateSet('both', 'claude', 'openrouter')] [string]$Show,
    [ValidateSet('window', 'widget', 'tray', 'none')] [string]$Launcher,
    [ValidateSet('yes', 'no')] [string]$Autostart
)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "python is required (https://www.python.org/downloads/, tick 'Add to PATH')."
    exit 1
}
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

function Ask($question, $choices, $default) {
    $answer = Read-Host "$question [$($choices -join '/')] (default $default)"
    if (-not $answer) { return $default }
    if ($choices -contains $answer.ToLower()) { return $answer.ToLower() }
    Write-Host "  '$answer' is not an option, using $default"; return $default
}
if (-not $Show) { $Show = Ask 'What to show' @('both', 'claude', 'openrouter') 'both' }
if (-not $Launcher) { $Launcher = Ask 'Launcher' @('window', 'widget', 'tray', 'none') 'widget' }
if (-not $Autostart -and $Launcher -ne 'none') { $Autostart = Ask 'Start it with Windows' @('yes', 'no') 'no' }

# 1. Saved preference
& $python "$dir\quotaline.py" --show $Show --save

# 2. Claude Code status line
if (Get-Command claude -ErrorAction SilentlyContinue) {
    & $python "$dir\quotaline.py" --install-statusline --show $Show
} else {
    Write-Host "Claude Code is not installed on this machine: status line not configured."
}

# 3. Launcher shortcut (in this folder and on the Desktop)
if ($Launcher -ne 'none') {
    if ($Launcher -eq 'tray') {
        & $python -m pip install --quiet pystray pillow
        if ($LASTEXITCODE -ne 0) { Write-Host "pip install pystray pillow failed; the tray will not start."; }
    }
    $shell = New-Object -ComObject WScript.Shell
    $targets = @{
        window = @("$dir\quotaline.bat", '')
        widget = @($pythonw, "`"$dir\quotaline.py`" --widget")
        tray   = @($pythonw, "`"$dir\quotaline.py`" --tray")
    }
    $desktop = [Environment]::GetFolderPath('Desktop')
    $paths = @("$dir\quotaline.lnk", "$desktop\quotaline.lnk")
    if ($Autostart -eq 'yes') { $paths += (Join-Path ([Environment]::GetFolderPath('Startup')) 'quotaline.lnk') }
    foreach ($path in $paths) {
        $lnk = $shell.CreateShortcut($path)
        $lnk.TargetPath = $targets[$Launcher][0]
        $lnk.Arguments = $targets[$Launcher][1]
        $lnk.WorkingDirectory = $dir
        $lnk.IconLocation = "$dir\_icon.ico,0"
        $lnk.Description = "OpenRouter balance and Claude Code limits ($Launcher)"
        $lnk.Save()
        Write-Host "Shortcut: $path"
    }
    if ($Launcher -eq 'tray') {
        # Windows 11 hides new tray icons in the overflow: start the tray once and promote its icon to the bar.
        Start-Process -FilePath $pythonw -ArgumentList "`"$dir\quotaline.py`" --tray" | Out-Null
        Start-Sleep -Seconds 6
        $entries = Get-ChildItem 'HKCU:\Control Panel\NotifyIconSettings' -ErrorAction SilentlyContinue |
            Where-Object { (Get-ItemProperty $_.PSPath).ExecutablePath -ieq $pythonw }
        foreach ($e in $entries) { Set-ItemProperty -Path $e.PSPath -Name IsPromoted -Value 1 -Type DWord }
        Write-Host "Tray started and pinned to the visible corner ($($entries.Count) icon entry)."
    }
}

# 4. Check
& $python "$dir\quotaline.py"
