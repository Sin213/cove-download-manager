; Inno Setup script for Cove Download Manager (Windows)
; Invoked from build-windows-wine.sh via:
;   iscc /DAppVersion=X.Y.Z /DSourceDir=<abs dist\cove-download-manager> \
;        /DOutputDir=<abs release> /DIconFile=<abs cove_dm_icon.ico> installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\cove-download-manager"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#ifndef IconFile
  #define IconFile "..\cove_dm_icon.ico"
#endif

[Setup]
AppId={{F5EE4E1A-6A6C-4E89-9F64-29B49D3B0F31}
AppName=Cove Download Manager
AppVersion={#AppVersion}
AppPublisher=Cove
AppPublisherURL=https://github.com/Sin213/cove-download-manager
AppSupportURL=https://github.com/Sin213/cove-download-manager/issues
AppUpdatesURL=https://github.com/Sin213/cove-download-manager/releases
DefaultDirName={autopf}\Cove Download Manager
DefaultGroupName=Cove Download Manager
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\cove-download-manager.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Cove-Download-Manager-{#AppVersion}-Setup
SetupIconFile={#IconFile}
WizardStyle=modern
ChangesAssociations=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "magnetassoc"; Description: "Register Cove as a magnet link handler"; GroupDescription: "File associations:"

[Registry]
; Per-user only (HKCU). Cove is advertised as *capable* of opening magnet
; links; Windows still asks the user to pick the default under Default Apps.
; Nothing here overwrites another application's active association.
;
; Removal is deliberately NOT done with uninsdeletekey/uninsdeletevalue: the
; portable build registers the same ProgID and capability keys, so an
; unconditional delete would wipe a portable Cove's registration when this
; install is uninstalled. CurUninstallStepChanged below deletes only when the
; stored open command still points at this installation.
Root: HKCU; Subkey: "Software\Classes\Cove.Magnet"; ValueType: string; ValueName: ""; ValueData: "Magnet Link (Cove Download Manager)"; Tasks: magnetassoc
Root: HKCU; Subkey: "Software\Classes\Cove.Magnet"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""; Tasks: magnetassoc
Root: HKCU; Subkey: "Software\Classes\Cove.Magnet\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\cove-download-manager.exe"",0"; Tasks: magnetassoc
Root: HKCU; Subkey: "Software\Classes\Cove.Magnet\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\cove-download-manager.exe"" ""%1"""; Tasks: magnetassoc

Root: HKCU; Subkey: "Software\Cove\Cove Download Manager\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "Cove Download Manager"; Tasks: magnetassoc
Root: HKCU; Subkey: "Software\Cove\Cove Download Manager\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Multi-connection download manager with magnet link support"; Tasks: magnetassoc
Root: HKCU; Subkey: "Software\Cove\Cove Download Manager\Capabilities\URLAssociations"; ValueType: string; ValueName: "magnet"; ValueData: "Cove.Magnet"; Tasks: magnetassoc

Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "Cove Download Manager"; ValueData: "Software\Cove\Cove Download Manager\Capabilities"; Tasks: magnetassoc

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Cove Download Manager"; Filename: "{app}\cove-download-manager.exe"
Name: "{group}\Uninstall Cove Download Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Cove Download Manager"; Filename: "{app}\cove-download-manager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\cove-download-manager.exe"; Description: "Launch Cove Download Manager"; Flags: nowait postinstall skipifsilent

[Code]
const
  CoveProgIdKey = 'Software\Classes\Cove.Magnet';
  CoveCommandKey = 'Software\Classes\Cove.Magnet\shell\open\command';
  CoveCapabilitiesKey = 'Software\Cove\Cove Download Manager\Capabilities';
  CoveRegisteredAppsKey = 'Software\RegisteredApplications';
  CoveAppName = 'Cove Download Manager';

{ True only when the stored magnet open command still points at this
  installation. A portable Cove reuses the same ProgID, so uninstall must not
  delete a registration another Cove executable owns. }
function CoveOwnsMagnetRegistration(): Boolean;
var
  Command: String;
begin
  Result := False;
  if not RegQueryStringValue(HKEY_CURRENT_USER, CoveCommandKey, '', Command) then
    exit;
  Result := CompareText(Trim(Command),
    '"' + ExpandConstant('{app}\cove-download-manager.exe') + '" "%1"') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if CoveOwnsMagnetRegistration() then
    begin
      RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, CoveProgIdKey);
      RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, CoveCapabilitiesKey);
      RegDeleteValue(HKEY_CURRENT_USER, CoveRegisteredAppsKey, CoveAppName);
    end;
  end;
end;
