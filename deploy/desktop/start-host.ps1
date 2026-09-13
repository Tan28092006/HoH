$ErrorActionPreference = 'Stop'
# Ask the Windows management service to create the host outside the caller's
# process job. This matters when setup is run from Codex: restarting Codex then
# terminates its own job tree, but must not tear down the shared HoH endpoint.
$hostScript = @'
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
Start-Process -FilePath 'D:\HoH-service\.venv\Scripts\pythonw.exe' -ArgumentList @('"D:\HoH-service\hoh_desktop.py"', '--host') -WorkingDirectory 'D:\HoH-service' -WindowStyle Hidden
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($hostScript))
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$commandLine = '"' + $powershell + '" -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand ' + $encoded
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $commandLine }
if ($created.ReturnValue -ne 0) {
    throw "HoH detached host start failed with Win32 status $($created.ReturnValue)"
}
