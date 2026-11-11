; Inno Setup 6 script for TunerLab.
;
; Build the application first (dist\TunerLab\TunerLab.exe), then compile this
; file with ISCC.exe.  The installer is per-user (no admin prompt), which is what
; a workshop PC usually allows, and it never deletes calibration data on uninstall.
;
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss

#define MyAppName "TunerLab"
#ifndef MyAppVersion
#define MyAppVersion "0.5.12"
#endif
#define MyAppPublisher "TunerLab"
#define MyAppExeName "TunerLab.exe"

[Setup]
AppId={{8F3C2A64-6B1D-4E77-9E52-2B7A9C1D4F30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install: no elevation, installs under %LOCALAPPDATA%.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=TunerLab-{#MyAppVersion}-Setup
SetupIconFile=tunerlab.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "..\dist\TunerLab\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the frozen application's own cache; calibration data lives in Documents
; and is deliberately left alone.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // Nothing destructive: the operator's calibration databases and logs stay.
  end;
end;
