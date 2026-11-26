; Inno Setup script. Installs for the current user only, so no admin prompt.
; Built by GitHub Actions:  iscc /DMyAppVersion=0.1.0 packaging\installer.iss
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{6B0E8A1C-3C2D-4C7B-9E55-DE1C0E5A7A11}
AppName=Sample Manager
AppVersion={#MyAppVersion}
AppPublisher=Nigel's studio
DefaultDirName={localappdata}\Programs\Sample Manager
DefaultGroupName=Sample Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=SampleManager-Setup-{#MyAppVersion}
SetupIconFile=..\dsm\icon.ico
UninstallDisplayIcon={app}\SampleManager.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\SampleManager\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Sample Manager"; Filename: "{app}\SampleManager.exe"
Name: "{userdesktop}\Sample Manager"; Filename: "{app}\SampleManager.exe"; Tasks: desktopicon

[InstallDelete]
; before 0.9 the app was Deluge Sample Manager: take away its program and shortcuts (same AppId, so this updates it)
Type: files; Name: "{app}\DelugeSampleManager.exe"
Type: files; Name: "{group}\Deluge Sample Manager.lnk"
Type: files; Name: "{userdesktop}\Deluge Sample Manager.lnk"
Type: files; Name: "{userprograms}\Deluge Sample Manager.lnk"

[Run]
Filename: "{app}\SampleManager.exe"; Description: "Start Sample Manager"; Flags: nowait postinstall skipifsilent
; a silent install is an update from inside the app (dsm/updater.py): start the new version again
Filename: "{app}\SampleManager.exe"; Flags: nowait; Check: WizardSilent
