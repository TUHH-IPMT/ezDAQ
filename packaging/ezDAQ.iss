; packaging/ezDAQ.iss
;
; Inno Setup script for ezDAQ - builds a Windows installer around the
; PyInstaller onedir output in dist\ezDAQ. See the "Deployment" section
; of the README for the full sequence.
;
; Build:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\ezDAQ.iss
;         (a per-machine Inno Setup install puts ISCC.exe under
;         "C:\Program Files (x86)\Inno Setup 6" instead)
; Result: dist\ezDAQ-Setup-<version>.exe (at the project root)
;
; Every path below is written relative to THIS file's directory, which is
; what Inno Setup resolves relative paths against when no SourceDir is
; set - hence the leading "..\" for everything living at the project
; root. Deliberately not solved with `SourceDir=..`: that directive
; governs [Files] entries, and relying on it to also cover LicenseFile,
; SetupIconFile and OutputDir would be a guess about which directives it
; reaches.
;
; The NI-DAQmx driver is deliberately NOT part of this installer. It is a
; separate National Instruments system driver (administrator rights,
; typically a reboot) and may not be redistributed inside a third-party
; installer. Every machine needs it installed independently; ezDAQ starts
; without it and reports the missing driver in its device browser.

#define AppName "ezDAQ"
; Must match config/settings.py::APP_VERSION, which the About dialog
; shows. Inno Setup cannot import Python, so the value is duplicated
; here - tests/test_version.py fails if the two drift apart.
#define AppVersion "0.1"
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
; The user picks per-machine or per-user at the start of the wizard:
;   "for all users"  -> elevates, installs into Program Files, one copy
;                       for everyone. Right for a shared lab PC.
;   "for me only"    -> NO administrator rights needed, installs into
;                       %LOCALAPPDATA%\Programs\ezDAQ. Right when the
;                       user does not have admin on their own machine.
; Every path constant in this script is an "auto" one ({autopf},
; {autodesktop}, {group}), so all of them follow that choice by
; themselves - nothing else has to change between the two modes.
;
; Either way is safe because ezDAQ never writes next to its executable:
; configuration goes to %APPDATA%\ezDAQ (see
; config/settings.py::get_config_directory) and measurement data to a
; storage location the user picks.
;
; Note that the NI-DAQmx driver itself always needs administrator
; rights. A per-user install of ezDAQ therefore only removes the admin
; requirement for THIS application, not for putting a machine into a
; state where it can measure at all.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=ezDAQ-Setup-{#AppVersion}
; Stamps the version into the file properties of the setup .exe,
; so a deployed installer can be identified without running it.
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
SetupIconFile=..\resources\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The bundle is 64-bit (PyInstaller follows the Python that built it).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The complete PyInstaller onedir output. `recursesubdirs` picks up
; _internal\, which holds the interpreter, Qt, and the bundled
; resources\ directory.
Source: "..\dist\ezDAQ\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

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
