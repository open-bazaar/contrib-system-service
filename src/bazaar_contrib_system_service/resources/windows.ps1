# {{ managed_marker }}
$ErrorActionPreference = 'Stop'

$python = {{ python }}
$arguments = {{ arguments }}
$environmentScript = {{ environment_script }}
$logPath = {{ log_path }}

try {
    Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;

public static class BcnNoWindowProcess
{
    public static int Run(string python, string arguments, string logPath)
    {
        ProcessStartInfo startInfo = new ProcessStartInfo();
        startInfo.FileName = python;
        startInfo.Arguments = arguments;
        startInfo.UseShellExecute = false;
        startInfo.CreateNoWindow = true;
        startInfo.RedirectStandardInput = true;
        startInfo.RedirectStandardOutput = true;
        startInfo.RedirectStandardError = true;

        using (Process process = new Process())
        using (FileStream logStream = new FileStream(
            logPath,
            FileMode.Append,
            FileAccess.Write,
            FileShare.ReadWrite | FileShare.Delete
        ))
        {
            process.StartInfo = startInfo;
            process.Start();
            process.StandardInput.Close();

            object writeLock = new object();
            Task stdoutTask = CopyToLogAsync(
                process.StandardOutput.BaseStream,
                logStream,
                writeLock
            );
            Task stderrTask = CopyToLogAsync(
                process.StandardError.BaseStream,
                logStream,
                writeLock
            );

            process.WaitForExit();
            Task.WaitAll(stdoutTask, stderrTask);
            return process.ExitCode;
        }
    }

    private static async Task CopyToLogAsync(
        Stream source,
        Stream destination,
        object writeLock
    )
    {
        byte[] buffer = new byte[8192];
        int bytesRead;
        while ((bytesRead = await source.ReadAsync(buffer, 0, buffer.Length)) > 0)
        {
            lock (writeLock)
            {
                destination.Write(buffer, 0, bytesRead);
                destination.Flush();
            }
        }
    }
}
'@
} catch {
    $_ | Out-String | Add-Content -LiteralPath $logPath -Encoding utf8
    exit 1
}

if ($environmentScript -and (Test-Path -LiteralPath $environmentScript)) {
    try {
        . $environmentScript
    } catch {
        $_ | Out-String | Add-Content -LiteralPath $logPath -Encoding utf8
        exit 1
    }
}

$env:BCN_HOME = {{ home_dir }}

try {
    $exitCode = [BcnNoWindowProcess]::Run($python, $arguments, $logPath)
} catch {
    $_ | Out-String | Add-Content -LiteralPath $logPath -Encoding utf8
    exit 1
}
exit $exitCode
