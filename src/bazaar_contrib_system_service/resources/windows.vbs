' {{ managed_marker }}
Option Explicit

Dim shell
Dim command
Dim exitCode

Set shell = CreateObject("WScript.Shell")
command = {{ command }}
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
