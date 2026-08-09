; Inno Setup script for ZENITH/OS (Windows).
; Wraps the one-file ZENITH.exe in a per-user installer: no admin needed, Start
; Menu shortcut, optional desktop icon, and an optional "start at login" shortcut
; in the Startup folder (the Windows equivalent of launchd/systemd auto-start).
; Built in CI by ISCC.exe after PyInstaller produces dist\ZENITH.exe.

#define MyAppName "ZENITH"
; CI overrides this from the VERSION file via ISCC /DMyAppVersion=...
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#define MyAppPublisher "ZENITH/OS"
#define MyAppExeName "ZENITH.exe"

[Setup]
AppId={{2F9C7A54-7E3D-4C2B-9A1E-9D6C0F5B2A10}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ZENITH
DisableProgramGroupPage=yes
; Per-user install → no UAC/admin prompt.
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=ZENITH-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "startup"; Description: "Start ZENITH automatically when I log in"; GroupDescription: "Startup:"

[Files]
Source: "..\..\dist\ZENITH.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ZENITH"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\ZENITH"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\ZENITH"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ZENITH now"; Flags: nowait postinstall skipifsilent
