mod credential_prompt;
mod host;

use serde_json::Value;
use tauri::{AppHandle, Manager, State};

use host::{
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
async fn conversation_list(state: State<'_, HostState>) -> CommandResult<Value> {
    state.conversations().await.map_err(command_error)
}

#[tauri::command]
async fn memory_list(state: State<'_, HostState>) -> CommandResult<Value> {
    state.memories().await.map_err(command_error)
}

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
            desktop_status,
            conversation_list,
            conversation_history,
            conversation_submit,
            conversation_cancel,
            memory_list,
            memory_set_visibility,
            memory_forget,
            conversation_controls,
            conversation_control_update,
            conversation_controls_reset,
            provider_list,
            provider_create,
            provider_verify,
            provider_prompt_and_verify_credential,
            provider_prompt_and_rotate_credential,
        ])
        .run(tauri::generate_context!())
        .expect("run Project Sora Desktop");
}
