; BITFSAE CAN HOST Windows 安装包（Inno Setup 6）。
; 与软件内更新器（canhost/updater.py）的耦合约束：
; - 安装目录名必须等于 APP_FOLDER_NAME（BITFSAE_CAN_Host）：更新器整目录替换后
;   开始菜单/桌面快捷方式的绝对路径保持不变；
; - 必须每用户安装（PrivilegesRequired=lowest，目录在 %LOCALAPPDATA%\Programs）：
;   更新器替换安装目录时不请求管理员权限；
; - 软件内更新换目录后会从旧目录备份复制回 unins000.exe/.dat，
;   保证 Windows「应用和功能」的卸载入口不丢失。
; 命令行编译（build_windows.ps1 / CI 传入全部 define）：
;   ISCC.exe /DMyAppVersion=0.8.3 /DMyAppVersionLabel=0.8.3 ^
;            /DSourceDir=...\dist\BITFSAE_CAN_Host /DOutputDir=...\release ^
;            /DIconFile=...\app_icon.ico packaging\windows\canhost.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyAppVersionLabel
  #define MyAppVersionLabel MyAppVersion
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\BITFSAE_CAN_Host"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\release"
#endif
#ifndef IconFile
  #define IconFile "..\..\app_icon.ico"
#endif

[Setup]
AppId={{7C9E8B2A-4F5D-4C6E-9A3B-1D2E3F4A5B6C}
AppName=BITFSAE CAN Host
AppVersion={#MyAppVersionLabel}
AppVerName=BITFSAE CAN Host {#MyAppVersionLabel}
AppPublisher=BITFSAE
AppPublisherURL=https://github.com/BITFSAE
AppSupportURL=https://github.com/BITFSAE/can-host/issues
AppUpdatesURL=https://github.com/BITFSAE/can-host/releases
DefaultDirName={localappdata}\Programs\BITFSAE_CAN_Host
DefaultGroupName=BITFSAE CAN Host
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=BITFSAE_CAN_Host_v{#MyAppVersionLabel}_setup
SetupIconFile={#IconFile}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=BITFSAE CAN Host
UninstallDisplayIcon={app}\BITFSAE_CAN_Host.exe
VersionInfoVersion={#MyAppVersion}.0

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[InstallDelete]
; 软件内更新会整目录替换出与安装记录不一致的文件；重装/降级前清空目录，
; 避免新旧文件混合。CloseApplications 已保证此时应用未运行。
Name: "{app}"; Type: filesandordirs

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\BITFSAE CAN Host"; Filename: "{app}\BITFSAE_CAN_Host.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\BITFSAE CAN Host"; Filename: "{app}\BITFSAE_CAN_Host.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\BITFSAE_CAN_Host.exe"; WorkingDir: "{app}"; Description: "启动 BITFSAE CAN Host"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
; 卸载时清空整个安装目录：软件内更新换入的文件不在安装记录里，
; 否则目录会因残留文件无法删除。
Name: "{app}"; Type: filesandordirs

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // 软件内更新的旧版本备份（BITFSAE_CAN_Host.old-*）在安装目录的上一级，
  // 不在卸载记录里；卸载时一并清掉，避免残留占磁盘。
  if CurUninstallStep = usUninstall then
    DelTree(ExtractFilePath(ExcludeTrailingBackslash(ExpandConstant('{app}'))) +
      'BITFSAE_CAN_Host.old-*', True, True, True);
end;

