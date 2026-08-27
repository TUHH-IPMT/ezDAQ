; installer.iss
;
; Inno Setup script for ezDAQ - builds a Windows installer around the
; PyInstaller onedir output in dist\ezDAQ. See the "Deployment" section
; of the README for the full sequence.
;
; Build:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; Result: dist\ezDAQ-Setup-<version>.exe
;
; The NI-DAQmx driver is deliberately NOT part of this installer. It is a
; separate National Instruments system driver (administrator rights,
; typically a reboot) and may not be redistributed inside a third-party
; installer. Every machine needs it installed independently; ezDAQ starts
; without it and reports the missing driver in its device browser.

#define AppName "ezDAQ"
#define AppVersion "1.0.0"
#define AppPublisher "TUHH - Institut fuer Produktionsmanagement und -technik"
#define AppURL "https://github.com/TUHH-IPMT/ezDAQ"
#define AppExeName "ezDAQ.exe"

[Setup]
AppId={{8C1F2A54-0D3B-4E7A-9C21-6F5B2A9E4D18}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
; Per-machine install into Program Files, which is what a shared lab PC
; wants. Safe here because ezDAQ never writes next to its executable:
; configuration goes to %APPDATA%\ezDAQ (see
; config/settings.py::get_config_directory) and measurement data to a
; storage location the user picks.
PrivilegesRequired=admin
OutputDir=dist
OutputBaseFilename=ezDAQ-Setup-{#AppVersion}
SetupIconFile=resources\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The bundle is 64-bit (PyInstaller follows the Python that built it).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=LICENSE

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The complete PyInstaller onedir output. `recursesubdirs` picks up
; _internal\, which holds the interpreter, Qt, and the bundled
; resources\ directory.
Source: "dist\ezDAQ\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Nothing under {app} is written at runtime, so only the installed files
; are removed. %APPDATA%\ezDAQ is left alone on purpose: it holds the
; user's channel configurations, which should survive a reinstall.
Type: dirifempty; Name: "{app}"
