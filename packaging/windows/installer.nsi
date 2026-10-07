; ---------------------------------------------------------------------------
; Lofi — Windows installer (NSIS 3+, Unicode)
;
; Built by scripts/build.py with:
;   makensis /DAPPNAME=Lofi /DVERSION=1.0.2 /DARCH=amd64 ^
;            /DOUTFILE=dist\lofi-1.0.2-windows-amd64-setup.exe ^
;            /DBINARY=dist\lofi-windows-amd64.exe ^
;            /DREADME=README.md /DLICENSE=LICENSE /DEXAMPLE=config.example.json ^
;            packaging\windows\installer.nsi
;
; It installs the PyInstaller one-file binary to
;   C:\Program Files\Lofi\lofi.exe
; adds that directory to the machine PATH so `lofi --check` works from any
; prompt, creates Start Menu / Desktop shortcuts and registers an entry under
; "Apps & features" (ARP) so it can be uninstalled properly.
;
; System dependencies that are NOT bundled: ffmpeg. The installer does not
; fetch it (an installer that downloads things at install time is a supply-chain
; decision, not a packaging one); `lofi --check` tells the user what to install.
; ---------------------------------------------------------------------------

Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 64
AllowSkipFiles off
XPStyle on

!ifndef APPNAME
  !define APPNAME "Lofi"
!endif
!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef ARCH
  !define ARCH "amd64"
!endif
!ifndef PUBLISHER
  !define PUBLISHER "Lofi contributors"
!endif
!ifndef OUTDIRNAME
  !define OUTDIRNAME "Lofi"
!endif

Name "${APPNAME} ${VERSION}"
Caption "${APPNAME} ${VERSION} (${ARCH}) — Setup"
BrandingText "${APPNAME} ${VERSION} · github.com/mob5824m-wq/Discord-lofi"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\${OUTDIRNAME}"
InstallDirRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "InstallLocation"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show

; PE metadata so Explorer and ARP show a real version instead of 0.0.0.0
VIProductVersion "${VERSION}.0"
VIFileVersion    "${VERSION}.0"
VIAddVersionKey "ProductName"     "${APPNAME}"
VIAddVersionKey "ProductVersion"  "${VERSION}"
VIAddVersionKey "FileDescription" "${APPNAME} ${VERSION} (${ARCH}) Setup"
VIAddVersionKey "FileVersion"     "${VERSION}.0"
VIAddVersionKey "CompanyName"     "${PUBLISHER}"
VIAddVersionKey "LegalCopyright"  "Copyright (c) ${PUBLISHER}"

!include "MUI2.nsh"
!include "WinMessages.nsh"
!include "FileFunc.nsh"     ; ${GetSize}
!insertmacro GetSize

!define MUI_ABORTWARNING
!define MUI_UNABORTWARNING
!define MUI_FINISHPAGE_NOAUTOCLOSE
!define MUI_UNFINISHPAGE_NOAUTOCLOSE
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_NOTCHECKED
; `lofi` alone would start the bot, which fails without a token; the useful
; thing to run after installing is the diagnostic.
!define MUI_FINISHPAGE_RUN_FUNCTION "RunCheck"
!define MUI_FINISHPAGE_LINK "Setup guide (Discord token, permissions)"
!define MUI_FINISHPAGE_LINK_LOCATION "https://github.com/mob5824m-wq/Discord-lofi/blob/main/docs/SETUP.md"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${LICENSE}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------------------
; PATH helpers.
;
; The command is deliberately written without PowerShell variables (`$foo`),
; because `$` is NSIS's escape character and would have to be doubled. The
; back-quoted string lets the PowerShell snippet keep its own single quotes.
; ---------------------------------------------------------------------------
!macro AddToMachinePath Dir
  DetailPrint "Adding ${Dir} to the system PATH"
  nsExec::ExecToStack `powershell -NoProfile -NonInteractive -Command "if (-not ([Environment]::GetEnvironmentVariable('Path','Machine') -split ';' -contains '${Dir}')) { [Environment]::SetEnvironmentVariable('Path', ([Environment]::GetEnvironmentVariable('Path','Machine').TrimEnd(';') + ';${Dir}'), 'Machine') }"`
  Pop $0
  Pop $1
  SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
!macroend

!macro RemoveFromMachinePath Dir
  DetailPrint "Removing ${Dir} from the system PATH"
  nsExec::ExecToStack `powershell -NoProfile -NonInteractive -Command "[Environment]::SetEnvironmentVariable('Path', ([Environment]::GetEnvironmentVariable('Path','Machine').Replace('${Dir};','').Replace(';${Dir}','')), 'Machine')"`
  Pop $0
  Pop $1
  SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
!macroend

Function RunCheck
  ExecShell "open" "cmd.exe" '/K "$INSTDIR\lofi.exe" --check'
FunctionEnd

; ---------------------------------------------------------------------------
Section "Lofi (required)" SEC_MAIN
  SectionIn RO

  SetOutPath "$INSTDIR"

  ; Refuse to overwrite a running binary: Windows cannot delete it, and a
  ; half-unpacked install is worse than a clear message.
  IfFileExists "$INSTDIR\lofi.exe" 0 unpack
    Delete /REBOOTOK "$INSTDIR\lofi.exe"
    IfFileExists "$INSTDIR\lofi.exe" 0 unpack
      MessageBox MB_OK|MB_ICONSTOP "lofi.exe is in use.$\n$\nClose the running Lofi bot (and any prompt that has it open), then run this installer again." /SD IDOK
      Abort
  unpack:

  File /oname=lofi.exe "${BINARY}"
  File "${LICENSE}"
  File "${README}"
  File "${EXAMPLE}"

  ; PATH
  !insertmacro AddToMachinePath "$INSTDIR"

  ; Start Menu
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\Lofi --check.lnk" "$INSTDIR\lofi.exe" "--check" "$INSTDIR\lofi.exe" 0 SW_SHOWNORMAL "" "Check the Lofi install (ffmpeg, opus, token)"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\Lofi dashboard (demo).lnk" "$INSTDIR\lofi.exe" "--demo" "$INSTDIR\lofi.exe" 0 SW_SHOWNORMAL "" "Run the dashboard with simulated servers"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\Uninstall Lofi.lnk" "$INSTDIR\Uninstall.exe"

  ; Desktop shortcut — handy for a bot you start by hand.
  CreateShortCut "$DESKTOP\Lofi --check.lnk" "$INSTDIR\lofi.exe" "--check" "$INSTDIR\lofi.exe" 0 SW_SHOWNORMAL "" "Check the Lofi install"

  ; Uninstaller + "Apps & features" entry
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0

  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "DisplayName"     "${APPNAME} ${VERSION} (${ARCH})"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "DisplayIcon"     "$INSTDIR\lofi.exe,0"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "Publisher"       "${PUBLISHER}"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "HelpLink"        "https://github.com/mob5824m-wq/Discord-lofi/blob/main/docs/TROUBLESHOOTING.md"
  WriteRegStr   HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "URLInfoAbout"    "https://github.com/mob5824m-wq/Discord-lofi"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "EstimatedSize"   "$0"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "NoModify"        1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi" "NoRepair"        1
SectionEnd

Section "Uninstall"
  ; Never delete the data directory: it holds config.json with the bot token
  ; and lofi.db with the listening history. Tell the user where it is instead.
  !insertmacro RemoveFromMachinePath "$INSTDIR"

  Delete "$SMPROGRAMS\${APPNAME}\Lofi --check.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\Lofi dashboard (demo).lnk"
  Delete "$SMPROGRAMS\${APPNAME}\Uninstall Lofi.lnk"
  RMDir  "$SMPROGRAMS\${APPNAME}"
  Delete "$DESKTOP\Lofi --check.lnk"

  Delete "$INSTDIR\lofi.exe"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\config.example.json"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi"
SectionEnd
