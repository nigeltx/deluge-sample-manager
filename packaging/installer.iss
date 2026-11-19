; Inno Setup script. Installs for the current user only, so no admin prompt.
; Built by GitHub Actions:  iscc /DMyAppVersion=0.1.0 packaging\installer.iss
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{6B0E8A1C-3C2D-4C7B-9E55-DE1C0E5A7A11}
AppName=Deluge Sample Manager
AppVersion={#MyAppVersion}
AppPublisher=Nigel's studio
DefaultDirName={localappdata}\Programs\Deluge Sample Manager
DefaultGroupName=Deluge Sample Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=DelugeSampleManager-Setup-{#MyAppVersion}
SetupIconFile=..\dsm\icon.ico
UninstallDisplayIcon={app}\DelugeSampleManager.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\DelugeSampleManager\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Deluge Sample Manager"; Filename: "{app}\DelugeSampleManager.exe"
Name: "{userdesktop}\Deluge Sample Manager"; Filename: "{app}\DelugeSampleManager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DelugeSampleManager.exe"; Description: "Start Deluge Sample Manager"; Flags: nowait postinstall skipifsilent
