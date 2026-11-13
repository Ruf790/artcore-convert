; ArtCore Convert installer — Inno Setup 6.3+
; Build: run build.bat (FFmpeg + PyInstaller first, then this script).
; Expects the PyInstaller output (with ffmpeg\ inside) in dist\ArtCoreConvert\.

#define AppName      "ArtCore Convert"
#define AppVersion   "1.0.0"
#define AppPublisher "ArtCore"
#define AppURL       "https://github.com/Ruf790/artcore-convert"
#define AppExe       "ArtCoreConvert.exe"

[Setup]
; Never change AppId: Windows uses it to find the installed copy for updates/uninstall
AppId={{12DF62CC-D565-46D0-9EC7-F876FE9E351F}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=installer
OutputBaseFilename=ArtCoreConvert-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
AppCopyright=Copyright (C) 2026 Ruf790 — GNU GPL v3
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
AppMutex=ArtCoreConvertMutex
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto
UsedUserAreasWarning=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ru.GroupIntegration=Интеграция с Windows:
en.GroupIntegration=Windows integration:
ru.FileMenu=Пункт «Конвертировать в ArtCore Convert» в меню видеофайлов и папок
en.FileMenu="Convert with ArtCore Convert" in the menu of video files and folders
ru.ConvertIn=Конвертировать в ArtCore Convert
en.ConvertIn=Convert with ArtCore Convert

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "filemenu";    Description: "{cm:FileMenu}";          GroupDescription: "{cm:GroupIntegration}"

[Files]
Source: "dist\ArtCoreConvert\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "THIRD-PARTY.md"; DestDir: "{app}"; DestName: "THIRD-PARTY.txt"; Flags: ignoreversion

[InstallDelete]
; an older bundled FFmpeg goes away completely (its DLL names change between versions)
Type: filesandordirs; Name: "{app}\ffmpeg"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "ArtCore.Convert"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; HKA = HKLM for "all users", HKCU for "just me"
; "Convert with ArtCore Convert" for every file Windows treats as video, and for folders
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\video\shell\ArtCoreConvert"; ValueType: string; ValueName: ""; ValueData: "{cm:ConvertIn}"; Flags: uninsdeletekey; Tasks: filemenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\video\shell\ArtCoreConvert"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExe}"",0"; Tasks: filemenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\video\shell\ArtCoreConvert\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: filemenu
Root: HKA; Subkey: "Software\Classes\Directory\shell\ArtCoreConvert"; ValueType: string; ValueName: ""; ValueData: "{cm:ConvertIn}"; Flags: uninsdeletekey; Tasks: filemenu
Root: HKA; Subkey: "Software\Classes\Directory\shell\ArtCoreConvert"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExe}"",0"; Tasks: filemenu
Root: HKA; Subkey: "Software\Classes\Directory\shell\ArtCoreConvert\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: filemenu
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "NoOpenWith"; ValueData: ""

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; silent update from the program itself: start it again afterwards
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: WizardSilent

[UninstallDelete]
; Settings, presets and the queue in %APPDATA%\ArtCoreConvert are kept on purpose.
Type: filesandordirs; Name: "{app}"
