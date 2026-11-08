mod credential_prompt;
mod host;
mod profile_store;
mod notifications;
mod privacy;

use serde_json::Value;
use tauri::{AppHandle, Manager, State};

use host::{
    AdminExecuteRequest, AdminPreviewRequest, DebugSessionInfo, DebugFrameRequest, FrameObservationRequest,
    DesktopCancelTurnRequest, DesktopHistoryRequest, DesktopStatus, DesktopSubmitTurnRequest,
    HostError, HostState, ProviderCredentialPromptRequest, ProviderRotatePromptRequest, ShellError,
    TurnAccepted,
};

#[allow(dead_code, clippy::all)]
mod generated_contracts {
    include!(concat!(env!("OUT_DIR"), "/desktop_contracts.rs"));
}

type CommandResult<T> = Result<T, Box<ShellError>>;

fn command_error(error: HostError) -> Box<ShellError> {
    Box::new(error.into())
}

#[tauri::command]
async fn memory_maintenance_read(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<Value> {
    state.memory_maintenance_read(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn memory_maintenance_control(state: State<'_, HostState>, window: tauri::Window, request: host::MemoryMaintenanceRequest) -> CommandResult<Value> {
    state.memory_maintenance_control(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn narrative_read(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<Value> {
    state.narrative_read(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn narrative_action(state: State<'_, HostState>, window: tauri::Window, request: host::NarrativeActionRequest) -> CommandResult<Value> {
    state.narrative_action(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn companion_read(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<Value> {
    state.companion_read(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn companion_action(state: State<'_, HostState>, window: tauri::Window, request: host::CompanionActionRequest) -> CommandResult<Value> {
    state.companion_action(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn companion_governance(state: State<'_, HostState>, window: tauri::Window, domain: String) -> CommandResult<Value> {
    state.companion_governance(window.label(), &domain).await.map_err(command_error)
}

#[tauri::command]
async fn companion_session_open(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<DebugSessionInfo> {
    state.companion_session_open(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn companion_governance_resolve(state: State<'_, HostState>, window: tauri::Window, request: host::CompanionGovernanceRequest) -> CommandResult<Value> {
    state.companion_governance_resolve(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn privacy_export(state: State<'_, HostState>, window: tauri::Window, request: host::PrivacyExportRequest) -> CommandResult<Value> {
    #[cfg(target_os = "windows")]
    let native_owner = window.hwnd().map_err(|_| command_error(HostError::InvalidResponse))?.0 as usize;
    #[cfg(not(target_os = "windows"))]
    let native_owner = 0;
    state.privacy_export(window.label(), native_owner, request).await.map_err(command_error)
}

#[tauri::command]
async fn privacy_state(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<profile_store::PrivacyState> {
    state.privacy_state(window.label()).map_err(command_error)
}

#[tauri::command]
async fn privacy_erase(state: State<'_, HostState>, window: tauri::Window, app: AppHandle, request: host::PrivacyEraseRequest) -> CommandResult<()> {
    state.privacy_erase(window.label(), request).await.map_err(command_error)?;
    app.restart();
}

#[tauri::command]
async fn spaces_list(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<profile_store::SpacePage> {
    state.spaces_list(window.label()).map_err(command_error)
}

#[tauri::command]
async fn spaces_create(state: State<'_, HostState>, window: tauri::Window, name: String, expected_revision: String) -> CommandResult<profile_store::SpacePage> {
    state.spaces_create(window.label(), &name, &expected_revision).map_err(command_error)
}

#[tauri::command]
async fn spaces_open_location(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<()> {
    state.spaces_open_location(window.label()).map_err(command_error)
}

#[tauri::command]
async fn spaces_switch(state: State<'_, HostState>, window: tauri::Window, app: AppHandle, request: host::SpaceSwitchRequest) -> CommandResult<()> {
    state.spaces_switch(window.label(), request).await.map_err(command_error)?;
    app.restart();
}

#[tauri::command]
async fn notifications_settings(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<Value> { state.notifications_settings(window.label()).map_err(command_error) }
#[tauri::command]
async fn notifications_save(state: State<'_, HostState>, window: tauri::Window, request: host::NotificationSaveRequest) -> CommandResult<Value> { state.notifications_save(window.label(),request).map_err(command_error) }
#[tauri::command]
async fn notifications_test(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<Value> { state.notifications_test(window.label()).map_err(command_error) }

#[tauri::command]
async fn provider_bind_workload(state: State<'_, HostState>, request: host::ProviderBindingRequest) -> CommandResult<Value> {
    state.provider_bind_workload(request).await.map_err(command_error)
}

#[tauri::command]
async fn provider_delete(state: State<'_, HostState>, profile_id: String, confirmation: String) -> CommandResult<Value> {
    state.provider_delete(&profile_id, &confirmation).await.map_err(command_error)
}

#[tauri::command]
async fn desktop_status(state: State<'_, HostState>) -> CommandResult<DesktopStatus> {
    state.status().await.map_err(command_error)
}

#[tauri::command]
async fn conversation_history(
    state: State<'_, HostState>,
    request: DesktopHistoryRequest,
) -> CommandResult<Value> {
    state.history(request).await.map_err(command_error)
}

#[tauri::command]
async fn conversation_list(state: State<'_, HostState>, offset: Option<u32>, query: Option<String>) -> CommandResult<Value> {
    state.conversations(offset.unwrap_or(0), &query.unwrap_or_default()).await.map_err(command_error)
}

#[tauri::command]
async fn memory_list(state: State<'_, HostState>) -> CommandResult<Value> {
    state.memories().await.map_err(command_error)
}

#[tauri::command]
async fn memory_detail(state: State<'_, HostState>, memory_id: String) -> CommandResult<Value> { state.memory_detail(&memory_id).await.map_err(command_error) }

#[tauri::command]
async fn memory_correct(state: State<'_, HostState>, memory_id: String, request: host::MemoryCorrectionRequest) -> CommandResult<Value> { state.memory_correct(&memory_id, request).await.map_err(command_error) }

#[tauri::command]
async fn conversation_response_basis(state: State<'_, HostState>, conversation_id: String, turn_id: String) -> CommandResult<Value> { state.response_basis(&conversation_id, &turn_id).await.map_err(command_error) }

#[tauri::command]
async fn memory_set_visibility(
    state: State<'_, HostState>,
    memory_id: String,
    visible: bool,
) -> CommandResult<Value> {
    state
        .set_memory_visibility(&memory_id, visible)
        .await
        .map_err(command_error)
}

#[tauri::command]
async fn memory_forget(state: State<'_, HostState>, memory_id: String) -> CommandResult<Value> {
    state.forget_memory(&memory_id).await.map_err(command_error)
}

#[tauri::command]
async fn conversation_controls(
    state: State<'_, HostState>,
    conversation_id: String,
) -> CommandResult<Value> {
    state
        .conversation_controls(&conversation_id)
        .await
        .map_err(command_error)
}

#[tauri::command]
async fn conversation_control_update(
    state: State<'_, HostState>,
    conversation_id: String,
    request: Value,
) -> CommandResult<Value> {
    state
        .update_conversation_control(&conversation_id, request)
        .await
        .map_err(command_error)
}

#[tauri::command]
async fn conversation_controls_reset(
    state: State<'_, HostState>,
    conversation_id: String,
) -> CommandResult<Value> {
    state
        .reset_conversation_controls(&conversation_id)
        .await
        .map_err(command_error)
}

#[tauri::command]
async fn conversation_control_defaults(state: State<'_, HostState>, conversation_id: String, request: host::DefaultControlsRequest) -> CommandResult<Value> {
    state.conversation_control_defaults(&conversation_id, request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_snapshot(
    state: State<'_, HostState>,
    conversation_id: String,
) -> CommandResult<Value> {
    state
        .debug_snapshot(&conversation_id)
        .await
        .map_err(command_error)
}

#[tauri::command]
async fn debug_session_open(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<DebugSessionInfo> {
    state.debug_session_open(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn debug_review_session_open(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<DebugSessionInfo> {
    state.debug_review_session_open(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn debug_quality_session_open(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<DebugSessionInfo> {
    state.debug_quality_session_open(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn debug_quality_inspect(state: State<'_, HostState>, window: tauri::Window, request: host::QualityInspectRequest) -> CommandResult<Value> {
    state.debug_quality_inspect(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_quality_resolve(state: State<'_, HostState>, window: tauri::Window, request: host::QualityResolutionRequest) -> CommandResult<Value> {
    state.debug_quality_resolve(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_memory_governance(state: State<'_, HostState>, window: tauri::Window, kind: String, offset: Option<u32>) -> CommandResult<Value> {
    state.debug_memory_governance(window.label(), &kind, offset.unwrap_or(0)).await.map_err(command_error)
}

#[tauri::command]
async fn debug_memory_queue(state: State<'_, HostState>, window: tauri::Window, kind: String, status: String) -> CommandResult<Value> { state.debug_memory_queue(window.label(), &kind, &status).await.map_err(command_error) }

#[tauri::command]
async fn debug_review_resolve(state: State<'_, HostState>, window: tauri::Window, request: host::ReviewResolutionRequest) -> CommandResult<Value> {
    state.debug_review_resolve(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_quarantine_dismiss(state: State<'_, HostState>, window: tauri::Window, request: host::QuarantineDismissalRequest) -> CommandResult<Value> {
    state.debug_quarantine_dismiss(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_frame(state: State<'_, HostState>, window: tauri::Window, request: DebugFrameRequest) -> CommandResult<Value> {
    state.debug_frame(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_frame_observation(state: State<'_, HostState>, window: tauri::Window, request: FrameObservationRequest) -> CommandResult<Value> {
    state.debug_frame_observation(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_frame_observation_status(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<Value> {
    state.debug_frame_observation_status(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn debug_session_close(state: State<'_, HostState>, window: tauri::Window, session_id: String) -> CommandResult<Value> {
    state.debug_session_close(window.label(), &session_id).await.map_err(command_error)
}

#[tauri::command]
async fn debug_commands(state: State<'_, HostState>, window: tauri::Window) -> CommandResult<Value> {
    state.debug_commands(window.label()).await.map_err(command_error)
}

#[tauri::command]
async fn debug_command_preview(state: State<'_, HostState>, window: tauri::Window, request: AdminPreviewRequest) -> CommandResult<Value> {
    state.debug_command_preview(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_command_execute(state: State<'_, HostState>, window: tauri::Window, request: AdminExecuteRequest) -> CommandResult<Value> {
    state.debug_command_execute(window.label(), request).await.map_err(command_error)
}

#[tauri::command]
async fn debug_command_action(state: State<'_, HostState>, window: tauri::Window, action_id: String) -> CommandResult<Value> {
    state.debug_command_action(window.label(), &action_id).await.map_err(command_error)
}

#[tauri::command]
async fn conversation_submit(
    app: AppHandle,
    state: State<'_, HostState>,
    request: DesktopSubmitTurnRequest,
) -> CommandResult<TurnAccepted> {
    state.submit(app, request).await.map_err(command_error)
}

#[tauri::command]
async fn conversation_cancel(
    state: State<'_, HostState>,
    request: DesktopCancelTurnRequest,
) -> CommandResult<Value> {
    state.cancel(request).await.map_err(command_error)
}

#[tauri::command]
async fn provider_list(state: State<'_, HostState>) -> CommandResult<Value> {
    state.provider_list().await.map_err(command_error)
}

#[tauri::command]
async fn provider_create(state: State<'_, HostState>, request: Value) -> CommandResult<Value> {
    state.provider_create(request).await.map_err(command_error)
}

#[tauri::command]
async fn provider_verify(
    state: State<'_, HostState>,
    profile_id: String,
    request: Value,
) -> CommandResult<Value> {
    state
        .provider_verify(&profile_id, request)
        .await
        .map_err(command_error)
}

#[tauri::command]
async fn provider_prompt_and_verify_credential(
    state: State<'_, HostState>,
    request: ProviderCredentialPromptRequest,
) -> CommandResult<Value> {
    state
        .prompt_and_verify(request)
        .await
        .map_err(command_error)
}

#[tauri::command]
async fn provider_prompt_and_rotate_credential(
    state: State<'_, HostState>,
    request: ProviderRotatePromptRequest,
) -> CommandResult<Value> {
    state
        .prompt_and_rotate(request)
        .await
        .map_err(command_error)
}

pub fn run() {
    let state = HostState::new().expect("initialize Project Sora Desktop host");
    tauri::Builder::default()
        .manage(state)
        .on_window_event(|window, event| {
            if window.label() == "main"
                && matches!(
                    event,
                    tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed
                )
            {
                window.app_handle().exit(0);
            }
        })
        .invoke_handler(tauri::generate_handler![
            memory_maintenance_read,
            memory_maintenance_control,
            narrative_read,
            narrative_action,
            companion_read,
            companion_action,
            companion_governance,
            companion_session_open,
            companion_governance_resolve,
            desktop_status,
            spaces_list,
            spaces_create,
            spaces_open_location,
            spaces_switch,
            notifications_settings,
            notifications_save,
            notifications_test,
            privacy_export,
            privacy_state,
            privacy_erase,
            conversation_list,
            conversation_history,
            conversation_submit,
            conversation_cancel,
            memory_list,
            memory_detail,
            memory_correct,
            conversation_response_basis,
            memory_set_visibility,
            memory_forget,
            conversation_controls,
            conversation_control_update,
            conversation_controls_reset,
            conversation_control_defaults,
            debug_snapshot,
            debug_frame,
            debug_frame_observation,
            debug_frame_observation_status,
            debug_session_open,
            debug_review_session_open,
            debug_quality_session_open,
            debug_quality_inspect,
            debug_quality_resolve,
            debug_memory_governance,
            debug_memory_queue,
            debug_review_resolve,
            debug_quarantine_dismiss,
            debug_session_close,
            debug_commands,
            debug_command_preview,
            debug_command_execute,
            debug_command_action,
            provider_list,
            provider_create,
            provider_bind_workload,
            provider_delete,
            provider_verify,
            provider_prompt_and_verify_credential,
            provider_prompt_and_rotate_credential,
        ])
        .run(tauri::generate_context!())
        .expect("run Project Sora Desktop");
}
