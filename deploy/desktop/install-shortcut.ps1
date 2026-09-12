$ErrorActionPreference = 'Stop'
$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopPath 'HoH.lnk'
if (Test-Path -LiteralPath $shortcutPath) { throw 'An existing HoH desktop shortcut was preserved.' }
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap(64, 64)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.Clear([System.Drawing.Color]::FromArgb(110, 89, 235))
$font = New-Object System.Drawing.Font('Segoe UI', 35, [System.Drawing.FontStyle]::Bold)
$graphics.DrawString('H', $font, [System.Drawing.Brushes]::White, 10, 3)
$iconHandle = $bitmap.GetHicon()
$icon = [System.Drawing.Icon]::FromHandle($iconHandle)
$stream = [System.IO.File]::Create('D:\HoH-service\HoH.ico')
try { $icon.Save($stream) } finally { $stream.Dispose(); $font.Dispose(); $graphics.Dispose(); $bitmap.Dispose() }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'D:\HoH-service\.venv\Scripts\pythonw.exe'
$shortcut.Arguments = '"D:\HoH-service\hoh_desktop.py"'
$shortcut.WorkingDirectory = 'D:\HoH-service'
$shortcut.IconLocation = 'D:\HoH-service\HoH.ico,0'
$shortcut.Description = 'Start HoH services and open the local project dashboard'
$shortcut.WindowStyle = 7
$shortcut.Save()
$stopShortcut = $shell.CreateShortcut('D:\HoH-service\Stop HoH.lnk')
$stopShortcut.TargetPath = $shortcut.TargetPath
$stopShortcut.Arguments = '"D:\HoH-service\hoh_desktop.py" --stop'
$stopShortcut.WorkingDirectory = 'D:\HoH-service'
$stopShortcut.IconLocation = 'D:\HoH-service\HoH.ico,0'
$stopShortcut.WindowStyle = 7
$stopShortcut.Save()
Write-Output $shortcutPath
