# Kill stale Agent processes before a fresh launch.
# Only targets python.exe / pythonw.exe whose command line contains agent.app,
# so other Python programs on this machine are never touched.
# Kept ASCII-only on purpose: avoids encoding issues when saved from editors.

$targets = @('python.exe', 'pythonw.exe')
foreach ($name in $targets) {
    $procs = Get-CimInstance Win32_Process -Filter "Name = '$name'" -ErrorAction SilentlyContinue
    if ($procs) {
        $procs | Where-Object { $_.CommandLine -like '*agent.app*' } |
            ForEach-Object {
                try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
                catch { }
            }
    }
}
# 给一点时间让进程真正退出
Start-Sleep -Milliseconds 600
exit 0
