#ifndef AppVersion
  #define AppVersion "0.3.0"
#endif
[Setup]
AppId={{8E1803D2-3DA8-4955-9D0A-9BFB73E65B30}
AppName=DESKFLOW
AppVersion={#AppVersion}
AppPublisher=yefansky
DefaultDirName={localappdata}\Programs\DeskflowOrganizer
DefaultGroupName=DESKFLOW
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=DESKFLOW-windows-x64-Setup
SetupIconFile=..\assets\deskflow-icon-v1.ico
UninstallDisplayIcon={app}\DESKFLOW.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\DESKFLOW\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "prepare-install.ps1"; Flags: dontcopy
Source: "prepare-install.ps1"; DestDir: "{app}\_internal\packaging"; Flags: ignoreversion
Source: "legacy-mcp.py"; DestDir: "{app}\scripts"; DestName: "mcp_server.py"; Flags: ignoreversion
Source: "legacy-launch.py"; DestDir: "{app}\scripts"; DestName: "deskflow_manager.py"; Flags: ignoreversion
Source: "legacy-launch.py"; DestDir: "{app}\scripts"; DestName: "smart_desktop.py"; Flags: ignoreversion
Source: "legacy-uninstall.ps1"; DestDir: "{app}"; DestName: "uninstall.ps1"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\AI接入说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\MCP.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\VERSION"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userdesktop}\DESKFLOW 管理器"; Filename: "{app}\DESKFLOW.exe"; Parameters: "--start --show"
Name: "{group}\DESKFLOW"; Filename: "{app}\DESKFLOW.exe"; Parameters: "--start --show"
Name: "{group}\Uninstall DESKFLOW"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "Software\Classes\DesktopBackground\Shell\DESKFLOW"; ValueType: string; ValueData: "DESKFLOW"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\DesktopBackground\Shell\DESKFLOW"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\DESKFLOW.exe"
Root: HKCU; Subkey: "Software\Classes\DesktopBackground\Shell\DESKFLOW\command"; ValueType: string; ValueData: """{app}\DESKFLOW.exe"" --start --show"

[Run]
Filename: "{app}\DESKFLOW.exe"; Parameters: "--install"; Description: "Start DESKFLOW"; Flags: nowait postinstall skipifsilent
Filename: "{app}\DESKFLOW.exe"; Parameters: "--start"; Flags: nowait; Check: RestartRequested

[UninstallDelete]
Type: files; Name: "{userstartup}\DESKFLOW 启动器.bat"

[Code]
function RestartRequested: Boolean;
begin
  Result := ExpandConstant('{param:RESTARTAPP|0}') <> '0';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  ExtractTemporaryFile('prepare-install.ps1');
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\prepare-install.ps1') + '" -InstallRoot "' + ExpandConstant('{app}') + '"',
      '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    Result := 'Please exit DESKFLOW and disconnect its MCP server, then retry.';
end;

function InitializeUninstall: Boolean;
var Code: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\_internal\packaging\prepare-install.ps1') + '" -InstallRoot "' + ExpandConstant('{app}') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;
