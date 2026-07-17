; ═══════════════════════════════════════════════════════════════
;  HK Downloader Pro v3.3 - Inno Setup Installer Script
;  Creates a professional Windows installer that reduces
;  Windows SmartScreen false positive warnings.
; ═══════════════════════════════════════════════════════════════

#define AppName "HK Downloader Pro"
#define AppVersion "3.3"
#define AppPublisher "HK Downloader"
#define AppURL "https://github.com/ranarajput102221-collab/HKDownloaderPro"
#define AppExeName "HK Downloader Pro.exe"
#define AppSetupName "HK_Downloader_Pro_v3.3_Setup"
#define SourceExe "dist_v3.3\HK_Downloader_Pro_v3.3.exe"
#define SourceExtension "hk_extension"

[Setup]
; Basic Info
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}

; Output
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
OutputDir=dist_v3.3
OutputBaseFilename={#AppSetupName}

; Icons & Visual
SetupIconFile=logo.ico
WizardStyle=modern
WizardSizePercent=120

; Compression
Compression=lzma2/ultra64
SolidCompression=yes

; Privileges - request lowest (no admin needed, installs to user profile)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Uninstaller
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} v{#AppVersion}

; Version info shown in installer
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Installer
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

; Allow user to not create Start Menu shortcuts
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "startmenuicon"; Description: "Create Start Menu shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
; Main app executable
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "{#AppExeName}"; Flags: ignoreversion

; ffmpeg (required for video processing)
Source: "dist_v3.3\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion

; Chrome Extension folder
Source: "{#SourceExtension}\*"; DestDir: "{app}\hk_extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

; Desktop (only if user selected)
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Launch app after install
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up app data on uninstall (optional)
Type: filesandordirs; Name: "{app}"

[Code]
// Show a custom welcome message
procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption :=
    'This will install ' + ExpandConstant('{#AppName}') + ' version ' + ExpandConstant('{#AppVersion}') + ' on your computer.' + #13#10 + #13#10 +
    'Supported platforms:' + #13#10 +
    '  • TikTok   • Instagram   • YouTube' + #13#10 +
    '  • Facebook   • Pinterest' + #13#10 +
    '  • Douyin   • Kuaishou' + #13#10 + #13#10 +
    'Click Next to continue, or Cancel to exit.';
end;
