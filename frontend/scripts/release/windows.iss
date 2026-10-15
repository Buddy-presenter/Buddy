[Setup]
AppId={{CB25389A-3C26-4CB5-8D31-C4B9ABE61913}
AppName=Buddy
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Buddy
DefaultGroupName=Buddy
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDirectory}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\Buddy.exe
[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Buddy"; Filename: "{app}\Buddy.exe"
[Run]
Filename: "{app}\Buddy.exe"; Description: "Open Buddy"; Flags: nowait postinstall skipifsilent
