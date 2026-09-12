$ErrorActionPreference = 'Stop'
Start-Process -FilePath 'D:\HoH-service\.venv\Scripts\pythonw.exe' `
    -ArgumentList @('"D:\HoH-service\hoh_desktop.py"', '--host') `
    -WorkingDirectory 'D:\HoH-service' -WindowStyle Hidden
