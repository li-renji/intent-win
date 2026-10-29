# Kill stale Agent processes.
# Only targets python.exe whose command line contains agent.app, so other
# Python programs on this machine are never touched.
# Kept ASCII-only on purpose: avoids encoding issues when saved from editors.

$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue
if ($procs) {
    $procs | Where-Object { $_.CommandLine -like '*agent.app*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}
exit 0
