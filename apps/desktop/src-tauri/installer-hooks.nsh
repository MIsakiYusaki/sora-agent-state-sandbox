; Register only Sora's current-user notification identity. No permissions or
; system notification settings are changed; the runtime preference defaults off.
!macro NSIS_HOOK_POSTINSTALL
  WriteRegStr HKCU "Software\Classes\AppUserModelId\ai.chkong.sora" "DisplayName" "Project Sora"
  WriteRegStr HKCU "Software\Classes\AppUserModelId\ai.chkong.sora" "IconUri" "$INSTDIR\${MAINBINARYNAME}.exe"
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DeleteRegKey HKCU "Software\Classes\AppUserModelId\ai.chkong.sora"
!macroend
