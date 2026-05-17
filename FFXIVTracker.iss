; FFXIVTracker Inno Setup Script
; Compile via build_installer.py, or manually:
;   ISCC.exe /DMyAppVersion=v1.0.0 FFXIVTracker.iss

#ifndef MyAppVersion
  #define MyAppVersion "dev"
#endif
#ifndef MyStageDir
  #define MyStageDir "build\stage"
#endif
#ifndef MyOutputDir
  #define MyOutputDir "build"
#endif

#define MyAppName      "FFXIV Completion Tracker"
#define MyAppPublisher "JEschete"
#define MyAppURL       "https://github.com/JEschete/FFXIV_Completionist_Browser_App"

[Setup]
; NOTE: Replace this GUID if you fork or rebrand the app.
AppId={{A3F7C2D1-84B6-4E9A-B3F0-2C1D5E7A9B4F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
; Install to %LOCALAPPDATA% — no UAC prompt, no elevation required.
DefaultDirName={localappdata}\FFXIVTracker
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#MyOutputDir}
OutputBaseFilename=FFXIVTracker-{#MyAppVersion}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#MyStageDir}\assets\icon.ico
LicenseFile={#MyStageDir}\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Everything staged by build_installer.py (app source + python\ embed)
Source: "{#MyStageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; GUI shortcut — runs pythonw so no console window appears.
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\launch_gui.cmd"; \
  IconFilename: "{app}\assets\icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\launch_gui.cmd"; \
  IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon
; CLI menu shortcut for power users / troubleshooting.
Name: "{autoprograms}\{#MyAppName} (Text Menu)"; Filename: "{app}\launch.cmd"; \
  Parameters: "--cli"; IconFilename: "{app}\assets\icon.ico"

[Run]
Filename: "{app}\launch_gui.cmd"; \
  Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
  Flags: nowait postinstall shellexec skipifsilent
