[Setup]
AppId={{CB25389A-3C26-4CB5-8D31-C4B9ABE61913}
AppName=Buddy
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Buddy
DefaultGroupName=Buddy
PrivilegesRequired=lowest
ChangesAssociations=yes
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
[Registry]
Root: HKCU; Subkey: "Software\Classes\org.buddy.desktop"; ValueType: string; ValueName: ""; ValueData: "URL:Buddy sign-in"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\org.buddy.desktop"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\org.buddy.desktop\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Buddy.exe"" --auth-callback ""%1"""
[Run]
Filename: "{app}\Buddy.exe"; Description: "Open Buddy"; Flags: nowait postinstall skipifsilent
