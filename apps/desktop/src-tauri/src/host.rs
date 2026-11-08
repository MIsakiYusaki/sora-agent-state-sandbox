use std::{
    env,
    ffi::OsString,
    fs::File,
    io::{self, BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    sync::atomic::{AtomicBool, Ordering},
    thread::{self, JoinHandle},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use futures_util::StreamExt;
use reqwest::{Method, StatusCode};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager};
use thiserror::Error;
use tokio::sync::Mutex;
use url::Url;
use uuid::Uuid;
use zeroize::Zeroizing;

pub type HostResult<T> = Result<T, HostError>;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryMaintenanceRequest { pub paused: bool, pub expected_revision: String, pub confirmed: bool }

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompanionActionRequest { pub action: String, pub payload: Value }

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NarrativeActionRequest {
    pub request_id: String, pub instance_id: String, pub expected_revision: u64,
    pub bundle_id: String, pub canon_version: String, pub action: String, pub choice_id: Option<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompanionGovernanceRequest {
    pub domain: String, pub action: String, pub target_id: String, pub expected_revision: String, pub confirmed: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SpaceSwitchRequest { pub space_id: String, pub expected_revision: String, pub confirmed: bool }
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NotificationSaveRequest { pub preferences: crate::profile_store::NotificationPreferences, pub expected_revision: String }

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PrivacyExportRequest {
    pub kind: crate::privacy::ExportKind,
    pub expected_profile_id: String,
    pub confirmed: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PrivacyEraseRequest {
    pub expected_profile_id: String,
    pub expected_revision: String,
    pub confirmation: String,
    pub confirmed: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DebugFrameRequest {
    pub conversation_id: String,
    pub turn_id: String,
    pub frame_id: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FrameObservationRequest { pub enabled: bool }

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderRouteTarget { pub provider_profile_id: String, pub model_id: String }

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderBindingRequest {
    pub workload: String,
    pub primary: ProviderRouteTarget,
    #[serde(default)] pub fallbacks: Vec<ProviderRouteTarget>,
    #[serde(default)] pub requirements: Vec<String>,
    #[serde(default)] pub cross_provider_fallback: bool,
    pub expected_revision: Option<u64>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewResolutionRequest {
    pub review_id: String,
    pub action: String,
    pub expected_revision: String,
    pub confirmed: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QuarantineDismissalRequest {
    pub quarantine_id: String,
    pub expected_revision: String,
    pub confirmed: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum QualityKind { Runs, Baselines, Events }

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualityInspectRequest {
    pub kind: QualityKind,
    pub record_id: Option<String>,
    pub baseline_id: Option<String>,
    pub case_offset: Option<u32>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum QualityResolution { Investigated, Superseded, FalsePositive }

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualityResolutionRequest {
    pub event_id: String,
    pub expected_revision: String,
    pub resolution: QualityResolution,
    pub confirmed: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DefaultControlsRequest {
    pub action: String,
    pub confirmed: bool,
    #[serde(skip_serializing_if = "Option::is_none")] pub reply_depth: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")] pub follow_up: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")] pub topic_shift: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")] pub memory_mode: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryCorrectionRequest { pub summary: String, pub expected_revision: String, pub confirmed: bool }

const PROTOCOL_VERSION: &str = "1.0";
const HOST_ORIGIN: &str = "sora-desktop://host";
const GATEWAY_BOOTSTRAP_TIMEOUT: Duration = Duration::from_secs(15);
const GATEWAY_BOOTSTRAP_MAX_BYTES: u64 = 64 * 1024;
const GATEWAY_SIDECAR_SHA256: &str = env!("SORA_GATEWAY_SHA256");
const GATEWAY_SIDECAR_SIZE: &str = env!("SORA_GATEWAY_SIZE");

#[derive(Debug, Error)]
pub enum HostError {
    #[error("Project Sora Gateway could not be started")]
    GatewayStart,
    #[error("Project Sora Gateway did not start in time")]
    GatewayStartTimeout,
    #[error("Project Sora Gateway returned an invalid bootstrap payload")]
    InvalidBootstrap,
    #[error("Project Sora Gateway installation failed its integrity check")]
    GatewayIntegrity,
    #[error("Project Sora Gateway is unavailable")]
    GatewayUnavailable,
    #[error("Project Sora Gateway is restarting")]
    GatewayRestarting,
    #[error("调试会话已关闭或过期，请重新开启；命令不会自动重发。")]
    DebugSessionExpired,
    #[error("The conversation was updated elsewhere: {message}")]
    ConversationVersionConflict {
        message: String,
        safe_details: Value,
    },
    #[error("The Gateway rejected this operation: {0}")]
    GatewayRejected(String),
    #[error("The Gateway protocol is not supported")]
    ProtocolMismatch,
    #[error("The credential prompt was cancelled")]
    CredentialCancelled,
    #[error("The Windows credential prompt failed with code {0}")]
    CredentialPrompt(u32),
    #[cfg(not(target_os = "windows"))]
    #[error("Native credential entry is not available on this platform")]
    UnsupportedPlatform,
    #[error("The response from the local service was invalid")]
    InvalidResponse,
}

#[derive(Debug, Clone, Serialize)]
pub struct ShellError {
    pub schema_version: &'static str,
    pub code: &'static str,
    pub message: String,
    pub retryable: bool,
    pub retry_after_ms: Option<u64>,
    pub correlation_id: Option<String>,
    pub action: &'static str,
    pub safe_details: Value,
}

impl From<HostError> for ShellError {
    fn from(error: HostError) -> Self {
        let (code, retryable, action) = match &error {
            HostError::GatewayStart
            | HostError::GatewayStartTimeout
            | HostError::GatewayUnavailable => ("GATEWAY_UNAVAILABLE", true, "restart_gateway"),
            HostError::GatewayRestarting => ("GATEWAY_RESTARTING", true, "restart_gateway"),
            HostError::ConversationVersionConflict { .. } => {
                ("TURN_CONFLICT", false, "reload_conversation")
            }
            HostError::GatewayIntegrity => ("GATEWAY_UNAVAILABLE", false, "none"),
            HostError::ProtocolMismatch => ("PROTOCOL_MISMATCH", false, "none"),
            HostError::CredentialCancelled => ("CREDENTIAL_CANCELLED", false, "none"),
            HostError::CredentialPrompt(_) => {
                ("CREDENTIAL_REJECTED", false, "open_provider_settings")
            }
            #[cfg(not(target_os = "windows"))]
            HostError::UnsupportedPlatform => {
                ("CREDENTIAL_REJECTED", false, "open_provider_settings")
            }
            HostError::GatewayRejected(_) => ("INVALID_REQUEST", false, "none"),
            HostError::DebugSessionExpired => ("INVALID_REQUEST", false, "none"),
            HostError::InvalidBootstrap | HostError::InvalidResponse => {
                ("INTERNAL_ERROR", true, "restart_gateway")
            }
        };
        let safe_details = match &error {
            HostError::ConversationVersionConflict { safe_details, .. } => safe_details.clone(),
            HostError::DebugSessionExpired => json!({"reason":"debug_session_expired"}),
            _ => json!({}),
        };
        Self {
            schema_version: "ShellError.v1",
            code,
            message: error.to_string(),
            retryable,
            retry_after_ms: None,
            correlation_id: None,
            action,
            safe_details,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct GatewayBootstrap {
    pub schema_version: String,
    pub base_url: String,
    pub capability_token: String,
    pub capability_id: String,
    pub protocol_version: String,
    pub expires_at_ms: u64,
    pub client_instance_id: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DesktopStatus {
    pub schema_version: &'static str,
    pub phase: &'static str,
    pub profile_id: String,
    pub client_instance_id: String,
    pub protocol_version: &'static str,
    pub gateway_ready: bool,
    pub provider_ready: bool,
    pub recoverable: bool,
    pub status_message: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DesktopSubmitTurnRequest {
    pub conversation_id: String,
    pub text: String,
    #[serde(default = "main_branch")]
    pub branch_id: String,
    pub expected_conversation_version: Option<u64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DesktopCancelTurnRequest {
    pub conversation_id: String,
    pub turn_id: String,
    #[serde(default = "cancel_reason")]
    pub reason: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DesktopHistoryRequest {
    pub conversation_id: String,
    #[serde(default = "history_limit")]
    pub limit: u16,
    pub before_sequence: Option<u64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ProviderCredentialPromptRequest {
    pub profile_id: String,
    pub verification: Value,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ProviderRotatePromptRequest {
    pub profile_id: String,
    pub verification: Value,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TurnAccepted {
    pub schema_version: String,
    pub protocol_version: String,
    pub turn_id: String,
    pub request_id: String,
    pub conversation_id: String,
    pub correlation_id: String,
    pub status: String,
    pub conversation_version: u64,
    pub idempotent_replay: bool,
    pub events_url: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GatewayEvent {
    pub schema_version: String,
    pub protocol_version: String,
    pub turn_id: String,
    pub sequence: u64,
    pub event_type: String,
    pub correlation_id: String,
    #[serde(default)]
    pub payload: Value,
    pub created_at_ms: u64,
    #[serde(default)]
    pub replayed: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct DesktopEventEnvelope {
    pub schema_version: &'static str,
    pub kind: &'static str,
    pub conversation_id: Option<String>,
    pub turn_id: Option<String>,
    pub payload: Value,
}

struct GatewayRuntime {
    child: Child,
    stdout_drain: Option<JoinHandle<()>>,
    bootstrap: GatewayBootstrap,
    process_group: Option<GatewayProcessGroup>,
}

struct GatewayLaunch {
    executable: PathBuf,
    prefix_args: Vec<OsString>,
    current_dir: Option<PathBuf>,
    environment: Vec<(OsString, OsString)>,
    verify_integrity: bool,
}

#[cfg(target_os = "windows")]
struct GatewayProcessGroup {
    handle: usize,
}

#[cfg(target_os = "windows")]
impl GatewayProcessGroup {
    fn attach(child: &Child) -> HostResult<Self> {
        use std::{ffi::c_void, mem::size_of, os::windows::io::AsRawHandle};
        use windows::{
            core::PCWSTR,
            Win32::{
                Foundation::{CloseHandle, HANDLE},
                System::JobObjects::{
                    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                },
            },
        };

        let job = unsafe { CreateJobObjectW(None, PCWSTR::null()) }
            .map_err(|_| HostError::GatewayStart)?;
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast::<c_void>(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
            .and_then(|_| AssignProcessToJobObject(job, HANDLE(child.as_raw_handle())).map(|_| ()))
        };
        if configured.is_err() {
            let _ = unsafe { CloseHandle(job) };
            return Err(HostError::GatewayStart);
        }
        Ok(Self {
            handle: job.0 as usize,
        })
    }
}

#[cfg(target_os = "windows")]
impl Drop for GatewayProcessGroup {
    fn drop(&mut self) {
        use windows::Win32::Foundation::{CloseHandle, HANDLE};

        let _ = unsafe { CloseHandle(HANDLE(self.handle as *mut core::ffi::c_void)) };
    }
}

#[cfg(not(target_os = "windows"))]
struct GatewayProcessGroup;

#[cfg(not(target_os = "windows"))]
impl GatewayProcessGroup {
    fn attach(_child: &Child) -> HostResult<Self> {
        Ok(Self)
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DebugSessionInfo {
    schema_version: String,
    session_id: String,
    window_label: String,
    expires_at_ms: u64,
    scopes: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DebugSessionReply {
    session: DebugSessionInfo,
    capability_token: String,
    capability_id: String,
}

struct NativeDebugSession {
    info: DebugSessionInfo,
    token: Zeroizing<String>,
    parent_capability_id: String,
    base_url: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AdminPreviewRequest {
    schema_version: String,
    command_id: String,
    params: Value,
    idempotency_key: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AdminExecuteRequest {
    schema_version: String,
    action_id: String,
    idempotency_key: String,
    expected_revision: String,
    confirmed: bool,
}

pub struct HostState {
    gateway: Mutex<Option<GatewayRuntime>>,
    debug_session: Mutex<Option<NativeDebugSession>>,
    http: reqwest::Client,
    profile_id: String,
    subject_id: String,
    agent_id: String,
    client_instance_id: String,
    profile_store: crate::profile_store::ProfileStore,
    transitioning: AtomicBool,
}

impl HostState {
    pub fn new() -> HostResult<Self> {
        let subject_id = env::var("SORA_SUBJECT_ID").unwrap_or_else(|_| "local-user".into());
        let agent_id = env::var("SORA_AGENT_ID").unwrap_or_else(|_| "sora".into());
        let initial = env::var("SORA_PROFILE_ID").unwrap_or_else(|_| "local".into());
        let profile_store = crate::profile_store::ProfileStore::from_environment(&subject_id, &agent_id, &initial).map_err(HostError::GatewayRejected)?;
        let profile_id = profile_store.active().map_err(HostError::GatewayRejected)?;
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(90))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| HostError::GatewayUnavailable)?;
        Ok(Self {
            gateway: Mutex::new(None),
            debug_session: Mutex::new(None),
            http,
            profile_id, subject_id, agent_id, profile_store, transitioning: AtomicBool::new(false),
            client_instance_id: format!("desktop_{}", Uuid::new_v4().simple()),
        })
    }

    async fn bootstrap(&self) -> HostResult<GatewayBootstrap> {
        if self.transitioning.load(Ordering::SeqCst) { return Err(HostError::GatewayRejected("正在切换独立空间，请等待客户端重新启动。".into())); }
        let mut guard = self.gateway.lock().await;
        if self.profile_store.pending_erasure().map_err(HostError::GatewayRejected)?.is_some() {
            return Err(HostError::GatewayRejected("空间删除尚未完成，请到设置中的数据与隐私继续恢复。".into()));
        }
        if self.transitioning.load(Ordering::SeqCst) { return Err(HostError::GatewayRejected("正在切换独立空间。".into())); }
        let restart = match guard.as_mut() {
            Some(runtime) => runtime
                .child
                .try_wait()
                .map_err(|_| HostError::GatewayUnavailable)?
                .is_some(),
            None => true,
        };
        if restart {
            *guard = Some(self.spawn_gateway()?);
        }
        Ok(guard.as_ref().expect("gateway exists").bootstrap.clone())
    }

    fn spawn_gateway(&self) -> HostResult<GatewayRuntime> {
        let launch = resolve_gateway_launch()?;
        if launch.verify_integrity {
            verify_gateway_integrity(&launch.executable)?;
        }
        let mut command = Command::new(&launch.executable);
        command.args(&launch.prefix_args);
        if let Some(current_dir) = &launch.current_dir {
            command.current_dir(current_dir);
        }
        for (name, value) in &launch.environment {
            command.env(name, value);
        }
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000);
        }
        let mut child = command
            .args([
                "--profile-id",
                &self.profile_id,
                "--subject-id",
                &self.subject_id,
                "--agent-id",
                &self.agent_id,
                "--client-instance-id",
                &self.client_instance_id,
                "--origin",
                HOST_ORIGIN,
                "--log-level",
                "warning",
                "--no-legacy-migration",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|_| HostError::GatewayStart)?;
        let mut process_group = match GatewayProcessGroup::attach(&child) {
            Ok(process_group) => Some(process_group),
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        let stdout = child.stdout.take().ok_or(HostError::GatewayStart)?;
        let (bootstrap_tx, bootstrap_rx) = mpsc::sync_channel(1);
        let stdout_drain = thread::Builder::new()
            .name("sora-gateway-stdout".into())
            .spawn(move || {
                let mut reader = BufReader::new(stdout);
                let mut bytes = Vec::with_capacity(4 * 1024);
                let read_result = reader
                    .by_ref()
                    .take(GATEWAY_BOOTSTRAP_MAX_BYTES + 1)
                    .read_until(b'\n', &mut bytes)
                    .map_err(|_| HostError::GatewayStart)
                    .and_then(|_| {
                        if bytes.len() as u64 > GATEWAY_BOOTSTRAP_MAX_BYTES
                            || !bytes.ends_with(b"\n")
                        {
                            return Err(HostError::InvalidBootstrap);
                        }
                        String::from_utf8(bytes).map_err(|_| HostError::InvalidBootstrap)
                    });
                let _ = bootstrap_tx.send(read_result);
                let _ = io::copy(&mut reader, &mut io::sink());
            })
            .map_err(|_| HostError::GatewayStart)?;
        let line = match bootstrap_rx.recv_timeout(GATEWAY_BOOTSTRAP_TIMEOUT) {
            Ok(Ok(line)) => line,
            Ok(Err(error)) => {
                stop_gateway_process(&mut child, &mut process_group, stdout_drain);
                return Err(error);
            }
            Err(_) => {
                stop_gateway_process(&mut child, &mut process_group, stdout_drain);
                return Err(HostError::GatewayStartTimeout);
            }
        };
        let bootstrap: GatewayBootstrap = match serde_json::from_str(line.trim()) {
            Ok(bootstrap) => bootstrap,
            Err(_) => {
                stop_gateway_process(&mut child, &mut process_group, stdout_drain);
                return Err(HostError::InvalidBootstrap);
            }
        };
        if let Err(error) = validate_bootstrap(&bootstrap) {
            stop_gateway_process(&mut child, &mut process_group, stdout_drain);
            return Err(error);
        }
        Ok(GatewayRuntime {
            child,
            stdout_drain: Some(stdout_drain),
            bootstrap,
            process_group,
        })
    }

    fn main_window(window: &str) -> HostResult<()> {
        if window != "main" { return Err(HostError::GatewayRejected("此窗口不能管理独立空间。".into())); }
        Ok(())
    }

    pub fn spaces_list(&self, window: &str) -> HostResult<crate::profile_store::SpacePage> {
        Self::main_window(window)?;
        self.profile_store.list().map_err(HostError::GatewayRejected)
    }

    pub fn spaces_create(&self, window: &str, name: &str, expected: &str) -> HostResult<crate::profile_store::SpacePage> {
        Self::main_window(window)?;
        if self.transitioning.load(Ordering::SeqCst) { return Err(HostError::GatewayRejected("空间正在切换。".into())); }
        self.profile_store.create(name, expected).map_err(HostError::GatewayRejected)
    }

    pub fn spaces_open_location(&self, window: &str) -> HostResult<()> {
        Self::main_window(window)?;
        let path = self.profile_store.identity_directory(&self.profile_id).map_err(HostError::GatewayRejected)?;
        if !path.is_dir() { return Err(HostError::GatewayRejected("当前空间尚无运行数据，请先连接客户端。".into())); }
        #[cfg(target_os = "windows")]
        { Command::new("explorer.exe").arg(path).spawn().map_err(|_| HostError::GatewayRejected("无法打开数据位置。".into()))?; }
        #[cfg(not(target_os = "windows"))]
        { return Err(HostError::GatewayRejected("当前平台不支持打开Windows数据位置。".into())); }
        Ok(())
    }

    pub async fn spaces_switch(&self, window: &str, request: SpaceSwitchRequest) -> HostResult<()> {
        Self::main_window(window)?;
        if !request.confirmed { return Err(HostError::GatewayRejected("请确认切换并重启客户端。".into())); }
        if request.space_id == self.profile_id { return Err(HostError::GatewayRejected("已经在当前空间。".into())); }
        self.profile_store.validate_selection(&request.space_id, &request.expected_revision).map_err(HostError::GatewayRejected)?;
        let bootstrap = self.bootstrap().await?;
        if self.transitioning.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() { return Err(HostError::GatewayRejected("空间正在切换。".into())); }
        let result = self.finish_space_switch(bootstrap, &request).await;
        if result.is_err() { self.transitioning.store(false, Ordering::SeqCst); }
        result
    }

    async fn finish_space_switch(&self, bootstrap: GatewayBootstrap, request: &SpaceSwitchRequest) -> HostResult<()> {
        self.drain_gateway(&bootstrap).await?;
        self.profile_store.select(&request.space_id, &request.expected_revision).map_err(HostError::GatewayRejected)
    }

    async fn drain_gateway(&self, bootstrap: &GatewayBootstrap) -> HostResult<()> {
        // Drain atomically fences new turns in Gateway. Existing work may finish;
        // only after the old process exits may the new identity be persisted.
        let base = validated_loopback_url(&bootstrap.base_url)?;
        let response = self.http.post(join_loopback_url(&base, "/v1/admin/shutdown")?)
            .header("Authorization", format!("Bearer {}", bootstrap.capability_token))
            .header("X-Sora-Client-Instance", &bootstrap.client_instance_id)
            .header("X-Sora-Protocol-Version", PROTOCOL_VERSION).header("Origin", HOST_ORIGIN)
            .timeout(Duration::from_secs(5)).send().await.map_err(|_| HostError::GatewayUnavailable)?;
        if !response.status().is_success() { return Err(HostError::GatewayUnavailable); }
        let deadline = tokio::time::Instant::now() + Duration::from_secs(35);
        let mut guard = self.gateway.lock().await;
        if let Some(runtime) = guard.as_mut() {
            loop {
                if runtime.child.try_wait().map_err(|_| HostError::GatewayUnavailable)?.is_some() { break; }
                if tokio::time::Instant::now() >= deadline { return Err(HostError::GatewayRejected("旧空间仍在安全停止，尚未切换。请稍后重新读取并重试。".into())); }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        }
        if let Some(mut runtime) = guard.take() { stop_gateway_runtime(&mut runtime); }
        drop(guard);
        *self.debug_session.lock().await = None;
        Ok(())
    }

    pub async fn status(&self) -> HostResult<DesktopStatus> {
        if self.profile_store.pending_erasure().map_err(HostError::GatewayRejected)?.is_some() {
            return Ok(DesktopStatus {
                schema_version: "DesktopStatus.v1", phase: "degraded", profile_id: self.profile_id.clone(),
                client_instance_id: self.client_instance_id.clone(), protocol_version: PROTOCOL_VERSION,
                gateway_ready: false, provider_ready: false, recoverable: true,
                status_message: Some("空间删除尚未完成。请打开设置中的数据与隐私，继续恢复删除。".into()),
            });
        }
        let bootstrap = self.bootstrap().await?;
        let health: Value = self
            .request(Method::GET, "/v1/model-providers/health", None)
            .await?;
        let provider_ready = health
            .get("conversation_ready")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        Ok(DesktopStatus {
            schema_version: "DesktopStatus.v1",
            phase: if provider_ready {
                "ready"
            } else {
                "provider_setup_required"
            },
            profile_id: self.profile_id.clone(),
            client_instance_id: bootstrap.client_instance_id,
            protocol_version: PROTOCOL_VERSION,
            gateway_ready: true,
            provider_ready,
            recoverable: true,
            status_message: (!provider_ready).then(|| "请先连接一个可用模型".into()),
        })
    }

    pub fn notifications_settings(&self, window: &str) -> HostResult<Value> {
        Self::main_window(window)?;
        let settings = self.profile_store.notification_settings(&self.profile_id).map_err(HostError::GatewayRejected)?;
        Ok(json!({"preferences":settings.preferences,"revision":settings.revision,"os_permission":crate::notifications::permission()}))
    }

    pub async fn privacy_export(&self, window: &str, native_owner: usize, request: PrivacyExportRequest) -> HostResult<Value> {
        Self::main_window(window)?;
        if !request.confirmed || request.expected_profile_id != self.profile_id {
            return Err(HostError::GatewayRejected("请在当前空间重新发起导出。".into()));
        }
        if self.transitioning.load(Ordering::SeqCst) || self.profile_store.pending_erasure().map_err(HostError::GatewayRejected)?.is_some() {
            return Err(HostError::GatewayRejected("请先完成当前空间操作后再导出。".into()));
        }
        let kind = request.kind;
        let destination = tauri::async_runtime::spawn_blocking(move || crate::privacy::choose_destination(kind, native_owner))
            .await.map_err(|_| HostError::InvalidResponse)??;
        let Some(destination) = destination else {
            return Ok(json!({"schema_version":"DesktopPrivacyExportReceipt.v1", "kind":kind, "status":"cancelled"}));
        };
        let payload: Value = self.request(Method::POST, "/v1/privacy/export", Some(json!({"kind":kind,"confirmed":true}))).await?;
        if payload.get("schema_version").and_then(Value::as_str) != Some("DesktopPrivacyExport.v1")
            || payload.get("kind").and_then(Value::as_str) != Some(kind.identifier()) {
            return Err(HostError::InvalidResponse);
        }
        let bytes = serde_json::to_vec_pretty(&payload).map_err(|_| HostError::InvalidResponse)?;
        if bytes.len() > 4 * 1024 * 1024 { return Err(HostError::InvalidResponse); }
        let byte_count = bytes.len();
        tauri::async_runtime::spawn_blocking(move || crate::privacy::save_export(&destination, &bytes))
            .await.map_err(|_| HostError::InvalidResponse)??;
        Ok(json!({"schema_version":"DesktopPrivacyExportReceipt.v1", "kind":kind,"status":"saved","bytes":byte_count}))
    }

    pub fn privacy_state(&self, window: &str) -> HostResult<crate::profile_store::PrivacyState> {
        Self::main_window(window)?;
        self.profile_store.privacy_state(&self.profile_id).map_err(HostError::GatewayRejected)
    }

    pub async fn privacy_erase(&self, window: &str, request: PrivacyEraseRequest) -> HostResult<()> {
        Self::main_window(window)?;
        if !request.confirmed || request.expected_profile_id != self.profile_id || request.confirmation != "删除当前空间" {
            return Err(HostError::GatewayRejected("请在当前空间输入完整确认文字后再删除。".into()));
        }
        if self.transitioning.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
            return Err(HostError::GatewayRejected("空间操作正在进行，请等待结果。".into()));
        }
        let result = self.finish_privacy_erase(&request).await;
        if result.is_err() { self.transitioning.store(false, Ordering::SeqCst); }
        result
    }

    async fn finish_privacy_erase(&self, request: &PrivacyEraseRequest) -> HostResult<()> {
        // Persist intent before any irreversible step. On restart this record
        // blocks bootstrap and exposes recovery without opening the old data.
        let pending = self.profile_store.begin_erasure(&self.profile_id, &request.expected_revision).map_err(HostError::GatewayRejected)?;
        let bootstrap = {
            let mut guard = self.gateway.lock().await;
            let running = if let Some(runtime) = guard.as_mut() {
                runtime.child.try_wait().map_err(|_| HostError::GatewayUnavailable)?.is_none()
            } else { false };
            if running {
                guard.as_ref().map(|runtime| runtime.bootstrap.clone())
            } else {
                if let Some(mut runtime) = guard.take() { stop_gateway_runtime(&mut runtime); }
                None
            }
        };
        if let Some(bootstrap) = bootstrap { self.drain_gateway(&bootstrap).await?; }
        *self.debug_session.lock().await = None;
        let profile = self.profile_id.clone();
        let subject = self.subject_id.clone();
        let agent = self.agent_id.clone();
        tauri::async_runtime::spawn_blocking(move || erase_with_gateway_helper(&profile, &subject, &agent))
            .await.map_err(|_| HostError::InvalidResponse)??;
        self.profile_store.complete_erasure(&pending.operation_id).map_err(HostError::GatewayRejected)
    }
    pub fn notifications_save(&self, window: &str, request: NotificationSaveRequest) -> HostResult<Value> {
        Self::main_window(window)?;
        self.profile_store.save_notifications(&self.profile_id,request.preferences,&request.expected_revision).map_err(HostError::GatewayRejected)?;
        self.notifications_settings(window)
    }
    pub fn notifications_test(&self, window: &str) -> HostResult<Value> {
        Self::main_window(window)?;
        let settings = self.profile_store.notification_settings(&self.profile_id).map_err(HostError::GatewayRejected)?;
        if !settings.preferences.enabled { return Ok(json!({"status":"disabled"})); }
        if !settings.preferences.allows(crate::notifications::minute_of_day()) { return Ok(json!({"status":"quiet"})); }
        crate::notifications::show().map_err(|_| HostError::GatewayRejected("Windows未接收通知，请检查系统通知设置。".into()))?;
        Ok(json!({"status":"submitted_to_windows"}))
    }

    pub async fn history(&self, request: DesktopHistoryRequest) -> HostResult<Value> {
        let mut path = format!(
            "/v1/conversations/{}/history?limit={}",
            encode_path(&request.conversation_id),
            request.limit.clamp(1, 200)
        );
        if let Some(sequence) = request.before_sequence {
            path.push_str(&format!("&before_sequence={sequence}"));
        }
        self.request(Method::GET, &path, None).await
    }

    pub async fn conversations(&self, offset: u32, query: &str) -> HostResult<Value> {
        if offset > 1_000_000 || query.chars().count() > 200 { return Err(HostError::InvalidResponse); }
        self.request(Method::GET, &format!("/v1/conversations?limit=100&offset={offset}&query={}", encode_path(query)), None)
            .await
    }

    pub async fn memories(&self) -> HostResult<Value> {
        self.request(Method::GET, "/v1/memories", None).await
    }

    pub async fn memory_detail(&self, memory_id: &str) -> HostResult<Value> {
        self.request(Method::GET, &format!("/v1/memories/{}/details", encode_path(memory_id)), None).await
    }

    pub async fn memory_correct(&self, memory_id: &str, request: MemoryCorrectionRequest) -> HostResult<Value> {
        if !request.confirmed { return Err(HostError::InvalidResponse); }
        self.request(Method::POST, &format!("/v1/memories/{}/correct", encode_path(memory_id)), Some(json!(request))).await
    }

    pub async fn response_basis(&self, conversation_id: &str, turn_id: &str) -> HostResult<Value> {
        self.request(Method::GET, &format!("/v1/conversations/{}/responses/{}/basis", encode_path(conversation_id), encode_path(turn_id)), None).await
    }

    pub async fn set_memory_visibility(&self, memory_id: &str, visible: bool) -> HostResult<Value> {
        self.request(
            Method::PUT,
            &format!("/v1/memories/{}/visibility", encode_path(memory_id)),
            Some(json!({ "visible": visible })),
        )
        .await
    }

    pub async fn forget_memory(&self, memory_id: &str) -> HostResult<Value> {
        self.request(
            Method::DELETE,
            &format!("/v1/memories/{}", encode_path(memory_id)),
            None,
        )
        .await
    }

    pub async fn conversation_controls(&self, conversation_id: &str) -> HostResult<Value> {
        self.request(
            Method::GET,
            &format!(
                "/v1/conversations/{}/controls",
                encode_path(conversation_id)
            ),
            None,
        )
        .await
    }

    pub async fn update_conversation_control(
        &self,
        conversation_id: &str,
        request: Value,
    ) -> HostResult<Value> {
        self.request(
            Method::PUT,
            &format!(
                "/v1/conversations/{}/controls",
                encode_path(conversation_id)
            ),
            Some(request),
        )
        .await
    }

    pub async fn reset_conversation_controls(&self, conversation_id: &str) -> HostResult<Value> {
        self.request(
            Method::DELETE,
            &format!(
                "/v1/conversations/{}/controls",
                encode_path(conversation_id)
            ),
            None,
        )
        .await
    }

    pub async fn conversation_control_defaults(&self, conversation_id: &str, request: DefaultControlsRequest) -> HostResult<Value> {
        if !request.confirmed { return Err(HostError::InvalidResponse); }
        self.request(Method::POST, &format!("/v1/conversations/{}/control-defaults", encode_path(conversation_id)), Some(json!(request))).await
    }

    pub async fn debug_snapshot(&self, conversation_id: &str) -> HostResult<Value> {
        self.request(
            Method::GET,
            &format!(
                "/v1/debug/snapshot?conversation_id={}",
                encode_path(conversation_id)
            ),
            None,
        )
        .await
    }

    pub async fn debug_frame(&self, window_label: &str, request: DebugFrameRequest) -> HostResult<Value> {
        if window_label != "main" {
            return Err(HostError::DebugSessionExpired);
        }
        self.request(Method::POST, "/v1/debug/frame", Some(json!(request))).await
    }

    pub async fn debug_frame_observation(&self, window_label: &str, request: FrameObservationRequest) -> HostResult<Value> {
        if window_label != "main" { return Err(HostError::DebugSessionExpired); }
        self.request(Method::POST, "/v1/debug/frame-observation", Some(json!(request))).await
    }

    pub async fn debug_frame_observation_status(&self, window_label: &str) -> HostResult<Value> {
        if window_label != "main" { return Err(HostError::InvalidResponse); }
        self.request(Method::GET, "/v1/debug/frame-observation", None).await
    }

    pub async fn submit(
        &self,
        app: AppHandle,
        request: DesktopSubmitTurnRequest,
    ) -> HostResult<TurnAccepted> {
        let request_id = format!("request_{}", Uuid::new_v4().simple());
        let client_message_id = format!("message_{}", Uuid::new_v4().simple());
        let correlation_id = Uuid::new_v4().simple().to_string();
        let accepted: TurnAccepted = self
            .request(
                Method::POST,
                "/v1/turns",
                Some(json!({
                    "schema_version": "TurnSubmit.v1",
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "conversation_id": request.conversation_id,
                    "client_message_id": client_message_id,
                    "client_instance_id": self.client_instance_id,
                    "text": request.text,
                    "branch_id": request.branch_id,
                    "expected_conversation_version": request.expected_conversation_version,
                    "correlation_id": correlation_id,
                })),
            )
            .await?;
        let bootstrap = self.bootstrap().await?;
        let http = self.http.clone();
        let accepted_for_stream = accepted.clone();
        let notification_store = self.profile_store.clone();
        let notification_profile = self.profile_id.clone();
        tokio::spawn(async move {
            if let Err(error) =
                stream_events(http, bootstrap, app.clone(), &accepted_for_stream, notification_store, notification_profile).await
            {
                let payload =
                    serde_json::to_value(ShellError::from(error)).unwrap_or_else(|_| json!({}));
                let _ = app.emit(
                    "sora://desktop-event",
                    DesktopEventEnvelope {
                        schema_version: "DesktopEventEnvelope.v1",
                        kind: "shell_error",
                        conversation_id: Some(accepted_for_stream.conversation_id.clone()),
                        turn_id: Some(accepted_for_stream.turn_id.clone()),
                        payload,
                    },
                );
            }
        });
        Ok(accepted)
    }

    pub async fn cancel(&self, request: DesktopCancelTurnRequest) -> HostResult<Value> {
        let response: Value = self
            .request(
                Method::POST,
                &format!("/v1/turns/{}/cancel", encode_path(&request.turn_id)),
                Some(json!({
                    "schema_version": "CancelRequest.v1",
                    "protocol_version": PROTOCOL_VERSION,
                    "reason": request.reason,
                })),
            )
            .await?;
        if response.get("conversation_id").and_then(Value::as_str)
            != Some(request.conversation_id.as_str())
        {
            return Err(HostError::InvalidResponse);
        }
        Ok(response)
    }

    pub async fn provider_list(&self) -> HostResult<Value> {
        let value: Value = self
            .request(Method::GET, "/v1/model-providers", None)
            .await?;
        let health: Value = self
            .request(Method::GET, "/v1/model-providers/health", None)
            .await?;
        Ok(json!({
            "revision": value.get("registry_revision").and_then(Value::as_u64).unwrap_or(0),
            "profiles": value.get("profiles").cloned().unwrap_or_else(|| json!([])),
            "models": value.get("models").cloned().unwrap_or_else(|| json!({})),
            "bindings": value.get("bindings").cloned().unwrap_or_else(|| json!([])),
            "health": health,
        }))
    }

    pub async fn provider_create(&self, request: Value) -> HostResult<Value> {
        self.request(Method::POST, "/v1/model-providers", Some(request))
            .await
    }

    pub async fn provider_bind_workload(&self, request: ProviderBindingRequest) -> HostResult<Value> {
        self.request(Method::PUT, &format!("/v1/model-provider-bindings/{}", encode_path(&request.workload)), Some(json!(request))).await
    }

    pub async fn provider_delete(&self, profile_id: &str, confirmation: &str) -> HostResult<Value> {
        if confirmation != format!("delete:{profile_id}") { return Err(HostError::GatewayRejected("删除模型配置需要明确确认。".into())); }
        self.request(Method::DELETE, &format!("/v1/model-providers/{}", encode_path(profile_id)), Some(json!({"confirmation":confirmation}))).await
    }

    pub async fn provider_verify(&self, profile_id: &str, request: Value) -> HostResult<Value> {
        self.request(
            Method::POST,
            &format!("/v1/model-providers/{}/verify", encode_path(profile_id)),
            Some(request),
        )
        .await
    }

    pub async fn prompt_and_verify(
        &self,
        request: ProviderCredentialPromptRequest,
    ) -> HostResult<Value> {
        let existing_profile = self.provider_profile(&request.profile_id).await?;
        let label = request
            .verification
            .get("model_id")
            .and_then(Value::as_str)
            .unwrap_or("Provider");
        let requires_secret =
            existing_profile.get("auth_scheme").and_then(Value::as_str) != Some("none");
        let has_secret = existing_profile
            .get("credential_ref")
            .and_then(Value::as_str)
            == Some("bound");
        if requires_secret && !has_secret {
            let secret = crate::credential_prompt::prompt_api_key(label)?;
            self.save_credential(&request.profile_id, &secret).await?;
        }
        let descriptor = self
            .provider_verify(&request.profile_id, request.verification.clone())
            .await?;
        let routing = self
            .setup_compatible_workloads(&request.profile_id, &descriptor)
            .await?;
        let profile = self.provider_profile(&request.profile_id).await?;
        Ok(json!({
            "schema_version": "DesktopProviderSetupOutcome.v1",
            "profile": profile,
            "routing": routing,
        }))
    }

    pub async fn prompt_and_rotate(
        &self,
        request: ProviderRotatePromptRequest,
    ) -> HostResult<Value> {
        let label = request
            .verification
            .get("model_id")
            .and_then(Value::as_str)
            .unwrap_or("Provider");
        let secret = crate::credential_prompt::prompt_api_key(label)?;
        let verification = request.verification;
        let body = json!({
            "credential": {"api_key": secret.as_str(), "headers": {}},
            "verification": verification.clone(),
        });
        let profile: Value = self
            .request(
                Method::POST,
                &format!(
                    "/v1/model-providers/{}/credential/rotate",
                    encode_path(&request.profile_id)
                ),
                Some(body),
            )
            .await?;
        let routing = self
            .setup_compatible_workloads(&request.profile_id, &verification)
            .await?;
        Ok(json!({
            "schema_version": "DesktopProviderSetupOutcome.v1",
            "profile": profile,
            "routing": routing,
        }))
    }

    async fn save_credential(
        &self,
        profile_id: &str,
        secret: &Zeroizing<String>,
    ) -> HostResult<Value> {
        self.request(
            Method::PUT,
            &format!("/v1/model-providers/{}/credential", encode_path(profile_id)),
            Some(json!({"api_key": secret.as_str(), "headers": {}})),
        )
        .await
    }

    async fn setup_compatible_workloads(
        &self,
        profile_id: &str,
        model: &Value,
    ) -> HostResult<Value> {
        let model_id = model
            .get("model_id")
            .and_then(Value::as_str)
            .ok_or(HostError::InvalidResponse)?;
        self.request(
            Method::POST,
            &format!(
                "/v1/model-providers/{}/setup-default-routes",
                encode_path(profile_id)
            ),
            Some(json!({"model_id": model_id})),
        )
        .await
    }

    async fn provider_profile(&self, profile_id: &str) -> HostResult<Value> {
        let configuration: Value = self
            .request(Method::GET, "/v1/model-providers", None)
            .await?;
        configuration
            .get("profiles")
            .and_then(Value::as_array)
            .and_then(|profiles| {
                profiles.iter().find(|profile| {
                    profile.get("profile_id").and_then(Value::as_str) == Some(profile_id)
                })
            })
            .cloned()
            .ok_or(HostError::InvalidResponse)
    }

    pub async fn debug_session_open(&self, window_label: &str) -> HostResult<DebugSessionInfo> {
        self.open_debug_session_at(window_label, "/v1/debug/sessions/open").await
    }

    pub async fn debug_review_session_open(&self, window_label: &str) -> HostResult<DebugSessionInfo> {
        self.open_debug_session_at(window_label, "/v1/debug/reviews/session").await
    }

    pub async fn debug_quality_session_open(&self, window_label: &str) -> HostResult<DebugSessionInfo> {
        self.open_debug_session_at(window_label, "/v1/debug/quality/session").await
    }

    pub async fn debug_quality_inspect(&self, window_label: &str, request: QualityInspectRequest) -> HostResult<Value> {
        let kind = match request.kind { QualityKind::Runs => "runs", QualityKind::Baselines => "baselines", QualityKind::Events => "events" };
        let offset = request.case_offset.unwrap_or(0);
        if offset > 1_000_000 { return Err(HostError::InvalidResponse); }
        let mut path = format!("/v1/debug/quality?kind={kind}&case_offset={offset}");
        for (key, value) in [("record_id", request.record_id), ("baseline_id", request.baseline_id)] {
            if let Some(value) = value {
                if value.is_empty() || value.len() > 128 { return Err(HostError::InvalidResponse); }
                path.push_str(&format!("&{key}={}", encode_path(&value)));
            }
        }
        self.debug_request(window_label, Method::GET, &path, None).await
    }

    pub async fn debug_quality_resolve(&self, window_label: &str, request: QualityResolutionRequest) -> HostResult<Value> {
        if !request.confirmed || request.expected_revision.len() != 64
            || !request.expected_revision.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) {
            return Err(HostError::GatewayRejected("请重新核对质量事件并明确确认结案。".into()));
        }
        self.debug_request(window_label, Method::POST, "/v1/debug/quality/resolve", Some(json!(request))).await
    }

    async fn open_debug_session_at(&self, window_label: &str, path: &str) -> HostResult<DebugSessionInfo> {
        if window_label != "main" {
            return Err(HostError::GatewayRejected("此窗口不能开启调试执行会话。".into()));
        }
        let mut guard = self.debug_session.lock().await;
        let bootstrap = self.bootstrap().await?;
        let reply: DebugSessionReply = self.request(Method::POST, path,
            Some(json!({"schema_version":"DesktopDebugSessionOpenRequest.v1", "window_label":window_label}))).await?;
        if reply.session.schema_version != "DesktopDebugSession.v1"
            || reply.session.window_label != window_label
            || !reply.session.session_id.starts_with("debug_")
            || reply.capability_token.len() < 32 || reply.capability_id.is_empty() {
            return Err(HostError::InvalidResponse);
        }
        let info = reply.session.clone();
        *guard = Some(NativeDebugSession {
            info: reply.session, token: Zeroizing::new(reply.capability_token),
            parent_capability_id: bootstrap.capability_id, base_url: bootstrap.base_url,
        });
        Ok(info)
    }

    pub async fn debug_session_close(&self, window_label: &str, session_id: &str) -> HostResult<Value> {
        let mut guard = self.debug_session.lock().await;
        if guard.as_ref().is_some_and(|session| session.info.window_label != window_label) {
            return Err(HostError::GatewayRejected("调试会话不属于此窗口。".into()));
        }
        if guard.as_ref().is_some_and(|session| session.info.session_id != session_id) {
            return Ok(json!({"closed":false}));
        }
        if let Some(session) = guard.take() {
            let base = validated_loopback_url(&session.base_url)?;
            let path = format!("/v1/debug/sessions/{}", encode_path(&session.info.session_id));
            // Closing a panel must not start a sidecar or retain credentials on
            // network failure. Revocation is best-effort; expiry still applies.
            let _ = self.http.delete(join_loopback_url(&base, &path)?)
                .header("Authorization", format!("Bearer {}", &*session.token))
                .header("X-Sora-Client-Instance", &self.client_instance_id)
                .header("X-Sora-Protocol-Version", PROTOCOL_VERSION).header("Origin", HOST_ORIGIN)
                .timeout(Duration::from_secs(2)).send().await;
        }
        Ok(json!({"closed":true}))
    }

    pub async fn debug_commands(&self, window_label: &str) -> HostResult<Value> {
        self.debug_request(window_label, Method::GET, "/v1/debug/commands", None).await
    }

    pub async fn companion_read(&self, window_label: &str) -> HostResult<Value> {
        if window_label != "main" { return Err(HostError::InvalidResponse); }
        self.request(Method::GET, "/v1/companion", None).await
    }

    pub async fn narrative_read(&self, window_label: &str) -> HostResult<Value> {
        if window_label != "main" { return Err(HostError::InvalidResponse); }
        self.request(Method::GET, "/v1/narrative", None).await
    }

    pub async fn memory_maintenance_read(&self, window_label: &str) -> HostResult<Value> {
        if window_label != "main" { return Err(HostError::InvalidResponse); }
        self.request(Method::GET, "/v1/memory/maintenance", None).await
    }

    pub async fn memory_maintenance_control(&self, window_label: &str, request: MemoryMaintenanceRequest) -> HostResult<Value> {
        if window_label != "main" || !request.confirmed || request.expected_revision.len() != 64 || !request.expected_revision.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err(HostError::InvalidResponse);
        }
        self.request(Method::POST, "/v1/memory/maintenance", Some(json!(request))).await
    }

    pub async fn narrative_action(&self, window_label: &str, request: NarrativeActionRequest) -> HostResult<Value> {
        if window_label != "main" || !["invite", "choice"].contains(&request.action.as_str()) || request.request_id.len() > 64 {
            return Err(HostError::InvalidResponse);
        }
        self.request(Method::POST, "/v1/narrative/actions", Some(json!(request))).await
    }

    pub async fn companion_action(&self, window_label: &str, request: CompanionActionRequest) -> HostResult<Value> {
        if window_label != "main" || !["controls", "agreements/propose", "agreements/confirm", "agreements/revoke", "repair"].contains(&request.action.as_str())
            || !request.payload.is_object() || request.payload.to_string().len() > 4096 {
            return Err(HostError::InvalidResponse);
        }
        self.request(Method::POST, &format!("/v1/companion/{}", request.action), Some(request.payload)).await
    }

    pub async fn companion_governance(&self, window_label: &str, domain: &str) -> HostResult<Value> {
        if window_label != "main" || !["persona", "relationship"].contains(&domain) { return Err(HostError::InvalidResponse); }
        self.request(Method::GET, &format!("/v1/debug/companion?domain={domain}"), None).await
    }

    pub async fn companion_session_open(&self, window_label: &str) -> HostResult<DebugSessionInfo> {
        self.open_debug_session_at(window_label, "/v1/debug/companion/session").await
    }

    pub async fn companion_governance_resolve(&self, window_label: &str, request: CompanionGovernanceRequest) -> HostResult<Value> {
        if !request.confirmed || !["persona", "relationship"].contains(&request.domain.as_str())
            || !["reject_candidate", "rollback_contribution"].contains(&request.action.as_str())
            || request.expected_revision.len() != 64
            || !request.expected_revision.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) {
            return Err(HostError::InvalidResponse);
        }
        self.debug_request(window_label, Method::POST, "/v1/debug/companion/resolve", Some(json!(request))).await
    }

    pub async fn debug_memory_governance(&self, window_label: &str, kind: &str, offset: u32) -> HostResult<Value> {
        if !["reviews", "candidates", "quarantines"].contains(&kind) || offset > 1_000_000 { return Err(HostError::InvalidResponse); }
        self.debug_request(window_label, Method::GET, &format!("/v1/debug/memory-governance?kind={kind}&offset={offset}"), None).await
    }

    pub async fn debug_memory_queue(&self, window_label: &str, kind: &str, status: &str) -> HostResult<Value> {
        if window_label != "main" || !["index", "projection"].contains(&kind) || !["all", "pending", "failed", "dead_letter"].contains(&status) { return Err(HostError::InvalidResponse); }
        self.request(Method::GET, &format!("/v1/debug/memory-queue?kind={kind}&status={status}"), None).await
    }

    pub async fn debug_review_resolve(&self, window_label: &str, request: ReviewResolutionRequest) -> HostResult<Value> {
        if !request.confirmed { return Err(HostError::GatewayRejected("请先核对审核目标并确认。".into())); }
        self.debug_request(window_label, Method::POST, "/v1/debug/memory-governance/resolve", Some(json!(request))).await
    }

    pub async fn debug_quarantine_dismiss(&self, window_label: &str, request: QuarantineDismissalRequest) -> HostResult<Value> {
        if !request.confirmed || request.expected_revision.len() != 64
            || !request.expected_revision.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) {
            return Err(HostError::GatewayRejected("请核对隔离记录并明确确认拒绝候选。".into()));
        }
        self.debug_request(window_label, Method::POST, "/v1/debug/memory-governance/quarantines/dismiss", Some(json!(request))).await
    }

    pub async fn debug_command_preview(&self, window_label: &str, request: AdminPreviewRequest) -> HostResult<Value> {
        self.debug_request(window_label, Method::POST, "/v1/debug/commands/preview", Some(json!(request))).await
    }

    pub async fn debug_command_execute(&self, window_label: &str, request: AdminExecuteRequest) -> HostResult<Value> {
        if !request.confirmed {
            return Err(HostError::GatewayRejected("请先确认预览影响。".into()));
        }
        self.debug_request(window_label, Method::POST, "/v1/debug/commands/execute", Some(json!(request))).await
    }

    pub async fn debug_command_action(&self, window_label: &str, action_id: &str) -> HostResult<Value> {
        self.debug_request(window_label, Method::GET,
            &format!("/v1/debug/commands/actions/{}", encode_path(action_id)), None).await
    }

    async fn debug_request(&self, window_label: &str, method: Method, path: &str, body: Option<Value>) -> HostResult<Value> {
        let bootstrap = self.bootstrap().await?;
        let mut guard = self.debug_session.lock().await;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64;
        let valid = guard.as_ref().is_some_and(|session|
            session.info.window_label == window_label && session.info.expires_at_ms > now
            && session.parent_capability_id == bootstrap.capability_id);
        if !valid {
            *guard = None;
            return Err(HostError::DebugSessionExpired);
        }
        let session = guard.as_ref().ok_or(HostError::InvalidResponse)?;
        let base = validated_loopback_url(&session.base_url)?;
        let mut request = self.http.request(method, join_loopback_url(&base, path)?)
            .header("Authorization", format!("Bearer {}", &*session.token))
            .header("X-Sora-Client-Instance", &self.client_instance_id)
            .header("X-Sora-Protocol-Version", PROTOCOL_VERSION).header("Origin", HOST_ORIGIN);
        if let Some(value) = body { request = request.json(&value); }
        let response = request.send().await.map_err(|_| HostError::GatewayUnavailable)?;
        let status = response.status();
        let bytes = response.bytes().await.map_err(|_| HostError::GatewayUnavailable)?;
        if status == StatusCode::UNAUTHORIZED { *guard = None; return Err(HostError::DebugSessionExpired); }
        if !status.is_success() { return Err(parse_gateway_error(status, &bytes)); }
        serde_json::from_slice(&bytes).map_err(|_| HostError::InvalidResponse)
    }

    async fn request<T: DeserializeOwned>(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
    ) -> HostResult<T> {
        let bootstrap = self.bootstrap().await?;
        let base = validated_loopback_url(&bootstrap.base_url)?;
        let url = join_loopback_url(&base, path)?;
        let mut request = self
            .http
            .request(method, url)
            .header(
                "Authorization",
                format!("Bearer {}", bootstrap.capability_token),
            )
            .header("X-Sora-Client-Instance", &bootstrap.client_instance_id)
            .header("X-Sora-Protocol-Version", PROTOCOL_VERSION)
            .header("Origin", HOST_ORIGIN);
        if let Some(value) = body {
            request = request.json(&value);
        }
        let response = request
            .send()
            .await
            .map_err(|_| HostError::GatewayUnavailable)?;
        let status = response.status();
        let bytes = response
            .bytes()
            .await
            .map_err(|_| HostError::GatewayUnavailable)?;
        if !status.is_success() {
            return Err(parse_gateway_error(status, &bytes));
        }
        serde_json::from_slice(&bytes).map_err(|_| HostError::InvalidResponse)
    }
}

impl Drop for HostState {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.gateway.try_lock() {
            if let Some(mut runtime) = guard.take() {
                stop_gateway_runtime(&mut runtime);
            }
        }
    }
}

async fn stream_events(
    http: reqwest::Client,
    bootstrap: GatewayBootstrap,
    app: AppHandle,
    accepted: &TurnAccepted,
    notification_store: crate::profile_store::ProfileStore,
    notification_profile: String,
) -> HostResult<()> {
    let base = validated_loopback_url(&bootstrap.base_url)?;
    let url = join_loopback_url(&base, &accepted.events_url)?;
    let response = http
        .get(url)
        .header(
            "Authorization",
            format!("Bearer {}", bootstrap.capability_token),
        )
        .header("X-Sora-Client-Instance", &bootstrap.client_instance_id)
        .header("X-Sora-Protocol-Version", PROTOCOL_VERSION)
        .header("Origin", HOST_ORIGIN)
        .send()
        .await
        .map_err(|_| HostError::GatewayUnavailable)?;
    if !response.status().is_success() {
        return Err(HostError::GatewayUnavailable);
    }
    let mut stream = response.bytes_stream();
    let mut buffer = String::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| HostError::GatewayUnavailable)?;
        buffer.push_str(&String::from_utf8_lossy(&chunk));
        while let Some(offset) = buffer.find("\n\n") {
            let frame = buffer[..offset].to_owned();
            buffer.drain(..offset + 2);
            if let Some(data) = frame.lines().find_map(|line| line.strip_prefix("data: ")) {
                let event: GatewayEvent =
                    serde_json::from_str(data).map_err(|_| HostError::InvalidResponse)?;
                let payload =
                    serde_json::to_value(&event).map_err(|_| HostError::InvalidResponse)?;
                app.emit(
                    "sora://desktop-event",
                    DesktopEventEnvelope {
                        schema_version: "DesktopEventEnvelope.v1",
                        kind: "gateway_event",
                        conversation_id: Some(accepted.conversation_id.clone()),
                        turn_id: Some(event.turn_id.clone()),
                        payload,
                    },
                )
                .map_err(|_| HostError::GatewayUnavailable)?;
                if event.event_type == "turn.committed" {
                    let focused = app.get_webview_window("main").is_none_or(|w| w.is_focused().unwrap_or(true));
                    if !focused {
                        if let Ok(settings) = notification_store.notification_settings(&notification_profile) {
                            if settings.preferences.allows(crate::notifications::minute_of_day()) { let _ = crate::notifications::show(); }
                        }
                    }
                }
                if matches!(
                    event.event_type.as_str(),
                    "turn.committed" | "turn.cancelled" | "turn.failed"
                ) {
                    return Ok(());
                }
            }
        }
    }
    Err(HostError::GatewayUnavailable)
}

#[cfg(debug_assertions)]
fn repository_root() -> HostResult<PathBuf> {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    manifest
        .ancestors()
        .nth(3)
        .map(Path::to_path_buf)
        .ok_or(HostError::GatewayStart)
}

fn resolve_gateway_launch() -> HostResult<GatewayLaunch> {
    #[cfg(debug_assertions)]
    {
        if let Some(executable) = env::var_os("SORA_GATEWAY_EXECUTABLE").map(PathBuf::from) {
            if !executable.is_file() {
                return Err(HostError::GatewayStart);
            }
            return Ok(GatewayLaunch {
                executable,
                prefix_args: Vec::new(),
                current_dir: None,
                environment: Vec::new(),
                verify_integrity: false,
            });
        }
        let root = repository_root()?;
        let python = env::var_os("SORA_PYTHON_EXECUTABLE")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join(".venv/Scripts/python.exe"));
        if !python.is_file() {
            return Err(HostError::GatewayStart);
        }
        Ok(GatewayLaunch {
            executable: python,
            prefix_args: vec![
                OsString::from("-m"),
                OsString::from("sora_sandbox.conversation_gateway.cli"),
            ],
            current_dir: Some(root.clone()),
            environment: vec![(
                OsString::from("PYTHONPATH"),
                root.join("src").into_os_string(),
            )],
            verify_integrity: false,
        })
    }

    #[cfg(not(debug_assertions))]
    {
        let executable = packaged_gateway_candidates()?
            .into_iter()
            .find(|candidate| candidate.is_file())
            .ok_or(HostError::GatewayStart)?;
        Ok(GatewayLaunch {
            executable,
            prefix_args: Vec::new(),
            current_dir: None,
            environment: Vec::new(),
            verify_integrity: true,
        })
    }
}

fn erase_with_gateway_helper(profile: &str, subject: &str, agent: &str) -> HostResult<()> {
    let launch = resolve_gateway_launch()?;
    if launch.verify_integrity { verify_gateway_integrity(&launch.executable)?; }
    let mut command = Command::new(&launch.executable);
    command.args(&launch.prefix_args);
    if let Some(directory) = &launch.current_dir { command.current_dir(directory); }
    for (name, value) in &launch.environment { command.env(name, value); }
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut child = command.args([
        "--erase-stopped-profile", "--profile-id", profile, "--subject-id", subject, "--agent-id", agent,
    ]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().map_err(|_| HostError::GatewayStart)?;
    let _group = match GatewayProcessGroup::attach(&child) {
        Ok(group) => group,
        Err(error) => { let _ = child.kill(); let _ = child.wait(); return Err(error); }
    };
    let input = serde_json::to_vec(&json!({"confirmed":true,"profile_id":profile,"confirmation":format!("erase:{profile}")}))
        .map_err(|_| HostError::InvalidResponse)?;
    let write_result = child.stdin.take().ok_or(HostError::InvalidResponse)?.write_all(&[input, vec![b'\n']].concat());
    if write_result.is_err() {
        let _ = child.kill(); let _ = child.wait();
        return Err(HostError::GatewayUnavailable);
    }
    let output = child.wait_with_output().map_err(|_| HostError::GatewayUnavailable)?;
    if !output.status.success() || output.stdout.len() > 8192 {
        return Err(HostError::GatewayRejected("删除尚未确认完成。空间保持关闭，请重新读取后继续恢复。".into()));
    }
    let receipt: Value = serde_json::from_slice(&output.stdout).map_err(|_| HostError::InvalidResponse)?;
    if receipt.get("schema_version").and_then(Value::as_str) != Some("ProfileErasure.v1")
        || receipt.get("status").and_then(Value::as_str) != Some("erased")
        || receipt.get("profile_id").and_then(Value::as_str) != Some(profile) {
        return Err(HostError::InvalidResponse);
    }
    Ok(())
}

#[cfg(not(debug_assertions))]
fn packaged_gateway_candidates() -> HostResult<Vec<PathBuf>> {
    let executable = env::current_exe().map_err(|_| HostError::GatewayStart)?;
    let directory = executable.parent().ok_or(HostError::GatewayStart)?;
    Ok(vec![
        directory.join("sora-gateway.exe"),
        directory.join("resources/sora-gateway.exe"),
        directory.join("../Resources/sora-gateway.exe"),
    ])
}

fn verify_gateway_integrity(path: &Path) -> HostResult<()> {
    let expected_size = GATEWAY_SIDECAR_SIZE
        .parse::<u64>()
        .map_err(|_| HostError::GatewayIntegrity)?;
    if GATEWAY_SIDECAR_SHA256.len() != 64 || expected_size == 0 {
        return Err(HostError::GatewayIntegrity);
    }
    let metadata = path.metadata().map_err(|_| HostError::GatewayIntegrity)?;
    if metadata.len() != expected_size {
        return Err(HostError::GatewayIntegrity);
    }
    let mut file = File::open(path).map_err(|_| HostError::GatewayIntegrity)?;
    let mut hasher = Sha256::new();
    io::copy(&mut file, &mut hasher).map_err(|_| HostError::GatewayIntegrity)?;
    let actual = format!("{:x}", hasher.finalize());
    if actual != GATEWAY_SIDECAR_SHA256 {
        return Err(HostError::GatewayIntegrity);
    }
    Ok(())
}

fn validate_bootstrap(bootstrap: &GatewayBootstrap) -> HostResult<()> {
    if bootstrap.schema_version != "ConversationGatewayBootstrap.v1"
        || bootstrap.protocol_version != PROTOCOL_VERSION
        || bootstrap.capability_token.len() < 32
        || bootstrap.capability_id.is_empty()
        || bootstrap.expires_at_ms == 0
    {
        return Err(HostError::InvalidBootstrap);
    }
    validated_loopback_url(&bootstrap.base_url)?;
    Ok(())
}

fn validated_loopback_url(value: &str) -> HostResult<Url> {
    let url = Url::parse(value).map_err(|_| HostError::InvalidBootstrap)?;
    let valid_host = matches!(url.host_str(), Some("127.0.0.1") | Some("localhost"));
    if url.scheme() != "http"
        || !valid_host
        || url.port().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(HostError::InvalidBootstrap);
    }
    Ok(url)
}

fn join_loopback_url(base: &Url, path: &str) -> HostResult<Url> {
    let target = base.join(path).map_err(|_| HostError::GatewayUnavailable)?;
    if target.scheme() != base.scheme()
        || target.host_str() != base.host_str()
        || target.port() != base.port()
        || !target.username().is_empty()
        || target.password().is_some()
    {
        return Err(HostError::GatewayUnavailable);
    }
    Ok(target)
}

fn stop_gateway_runtime(runtime: &mut GatewayRuntime) {
    runtime.process_group.take();
    let _ = runtime.child.kill();
    let _ = runtime.child.wait();
    if let Some(stdout_drain) = runtime.stdout_drain.take() {
        let _ = stdout_drain.join();
    }
}

fn stop_gateway_process(
    child: &mut Child,
    process_group: &mut Option<GatewayProcessGroup>,
    stdout_drain: JoinHandle<()>,
) {
    process_group.take();
    let _ = child.kill();
    let _ = child.wait();
    let _ = stdout_drain.join();
}

fn parse_gateway_error(status: StatusCode, bytes: &[u8]) -> HostError {
    let payload: Value = serde_json::from_slice(bytes).unwrap_or_else(|_| json!({}));
    let code = payload
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("UNKNOWN");
    if status == StatusCode::UPGRADE_REQUIRED || code == "PROTOCOL_UNSUPPORTED" {
        return HostError::ProtocolMismatch;
    }
    let message = payload
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("local gateway rejected the request");
    if code == "VERSION_CONFLICT" {
        return HostError::ConversationVersionConflict {
            message: message.to_owned(),
            safe_details: payload.get("details").cloned().unwrap_or_else(|| json!({})),
        };
    }
    if code == "DRAINING" {
        return HostError::GatewayRestarting;
    }
    HostError::GatewayRejected(format!("{code}: {message}"))
}

fn encode_path(value: &str) -> String {
    url::form_urlencoded::byte_serialize(value.as_bytes()).collect()
}

fn main_branch() -> String {
    "main".into()
}

fn cancel_reason() -> String {
    "user_cancelled".into()
}

fn history_limit() -> u16 {
    50
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "windows")]
    #[test]
    fn gateway_process_group_terminates_its_process_on_close() {
        use std::time::Instant;

        let mut child = Command::new("ping.exe")
            .args(["127.0.0.1", "-t"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn managed process");
        let process_group =
            GatewayProcessGroup::attach(&child).expect("attach managed process to job");

        drop(process_group);
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if child.try_wait().expect("query managed process").is_some() {
                break;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                panic!("managed process survived its Job Object");
            }
            thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn bootstrap_url_accepts_only_clean_loopback_origins() {
        assert!(validated_loopback_url("http://127.0.0.1:43123").is_ok());
        assert!(validated_loopback_url("http://localhost:43123").is_ok());

        for invalid in [
            "https://127.0.0.1:43123",
            "http://0.0.0.0:43123",
            "http://example.com:43123",
            "http://127.0.0.1",
            "http://user:secret@127.0.0.1:43123",
            "http://127.0.0.1:43123/v1",
            "http://127.0.0.1:43123?token=leak",
            "http://127.0.0.1:43123#fragment",
        ] {
            assert!(
                validated_loopback_url(invalid).is_err(),
                "unexpectedly accepted {invalid}"
            );
        }
    }

    #[test]
    fn joined_gateway_urls_cannot_escape_the_bootstrap_origin() {
        let base = validated_loopback_url("http://127.0.0.1:43123").expect("valid base");
        let joined = join_loopback_url(&base, "/v1/turns/turn_1/events").expect("join path");
        assert_eq!(
            joined.as_str(),
            "http://127.0.0.1:43123/v1/turns/turn_1/events"
        );

        for invalid in [
            "https://example.com/events",
            "http://127.0.0.1:9999/events",
            "http://localhost:43123/events",
            "http://user:secret@127.0.0.1:43123/events",
        ] {
            assert!(
                join_loopback_url(&base, invalid).is_err(),
                "unexpectedly joined {invalid}"
            );
        }
    }

    #[test]
    fn gateway_protocol_errors_are_not_retryable_local_failures() {
        let body = br#"{"code":"PROTOCOL_UNSUPPORTED","message":"upgrade required"}"#;
        assert!(matches!(
            parse_gateway_error(StatusCode::UPGRADE_REQUIRED, body),
            HostError::ProtocolMismatch
        ));
    }

    #[test]
    fn public_debug_session_has_no_native_credentials() {
        let reply: DebugSessionReply = serde_json::from_value(json!({
            "session": {"schema_version":"DesktopDebugSession.v1", "session_id":"debug_fixture",
                "window_label":"main", "expires_at_ms":300000, "scopes":["commands.maintenance"]},
            "capability_token":"native-only-secret-sentinel", "capability_id":"cap_fixture"
        })).unwrap();
        let public = serde_json::to_string(&reply.session).unwrap();
        assert!(!public.contains("capability_token"));
        assert!(!public.contains("native-only-secret-sentinel"));
        assert!(!public.contains("cap_fixture"));
    }

    #[test]
    fn space_switch_rejects_foreign_windows_and_implicit_restart_before_starting_runtime() {
        tauri::async_runtime::block_on(async {
            let state = HostState::new().unwrap();
            assert!(state.spaces_switch("foreign", SpaceSwitchRequest {space_id:"space_test".into(), expected_revision:"test".into(), confirmed:true}).await.is_err());
            assert!(state.spaces_switch("main", SpaceSwitchRequest {space_id:"space_test".into(), expected_revision:"test".into(), confirmed:false}).await.is_err());
            assert!(state.gateway.lock().await.is_none());
        });
    }

    #[test]
    fn erasure_requires_exact_current_identity_phrase_and_boolean_before_any_side_effect() {
        assert!(serde_json::from_value::<PrivacyEraseRequest>(json!({"expected_profile_id":"fixture","expected_revision":"a".repeat(64),"confirmation":"删除当前空间","confirmed":1})).is_err());
        assert!(serde_json::from_value::<PrivacyEraseRequest>(json!({"expected_profile_id":"fixture","expected_revision":"a".repeat(64),"confirmation":"删除当前空间","confirmed":true,"path":"C:/"})).is_err());
        tauri::async_runtime::block_on(async {
            let state = HostState::new().unwrap();
            let request = || PrivacyEraseRequest { expected_profile_id: state.profile_id.clone(), expected_revision: "a".repeat(64), confirmation: "删除当前空间".into(), confirmed: true };
            assert!(state.privacy_erase("foreign", request()).await.is_err());
            let mut implicit = request(); implicit.confirmed = false;
            assert!(state.privacy_erase("main", implicit).await.is_err());
            let mut wrong = request(); wrong.confirmation = "erase".into();
            assert!(state.privacy_erase("main", wrong).await.is_err());
            assert!(state.gateway.lock().await.is_none());
            assert!(!state.transitioning.load(Ordering::SeqCst));
        });
    }

    #[test]
    #[ignore = "Requires explicit isolated acceptance roots and the local Python Gateway"]
    fn isolated_host_gateway_erasure_and_restart_acceptance() {
        let root = PathBuf::from(env::var_os("SORA_ERASURE_ACCEPTANCE_ROOT").expect("explicit acceptance root"));
        assert!(root.is_absolute());
        let allowed = repository_root().unwrap().join("outputs").canonicalize().unwrap();
        assert!(root.canonicalize().unwrap().starts_with(allowed));
        assert_eq!(PathBuf::from(env::var_os("SORA_SANDBOX_CONFIG_DIR").unwrap()), root.join("config"));
        assert_eq!(PathBuf::from(env::var_os("SORA_GATEWAY_STATE_ROOT").unwrap()), root.join("runtime"));
        assert!(env::var("SORA_PROFILE_ID").unwrap().starts_with("qa-erasure-"));
        assert_eq!(env::var("SORA_SUBJECT_ID").unwrap(), "qa-erasure-user");
        tauri::async_runtime::block_on(async {
            let state = HostState::new().unwrap();
            let status = state.status().await.unwrap();
            assert_eq!(status.phase, "provider_setup_required");
            let exported: Value = state.request(Method::POST, "/v1/privacy/export", Some(json!({"kind":"memory_summary","confirmed":true}))).await.unwrap();
            assert_eq!(exported["content"]["exported_count"], 0);
            let page = state.spaces_list("main").unwrap();
            let page = state.spaces_create("main", "保留的隔离空间", &page.revision).unwrap();
            let neighbour = page.spaces[1].id.clone();
            let own_directory = state.profile_store.identity_directory(&state.profile_id).unwrap();
            let neighbour_directory = state.profile_store.identity_directory(&neighbour).unwrap();
            std::fs::create_dir_all(&neighbour_directory).unwrap();
            std::fs::write(neighbour_directory.join("retained-fixture.txt"), b"isolated-neighbour").unwrap();
            assert!(own_directory.join("memory.db").is_file());
            let privacy = state.privacy_state("main").unwrap();
            let old_profile = state.profile_id.clone();
            state.privacy_erase("main", PrivacyEraseRequest {
                expected_profile_id: old_profile.clone(), expected_revision: privacy.revision,
                confirmation: "删除当前空间".into(), confirmed: true,
            }).await.unwrap();
            assert!(!own_directory.exists());
            assert!(neighbour_directory.join("retained-fixture.txt").is_file());
            assert!(state.gateway.lock().await.is_none());
            assert!(state.bootstrap().await.is_err());
            let reopened = HostState::new().unwrap();
            assert_ne!(reopened.profile_id, old_profile);
            let privacy = reopened.privacy_state("main").unwrap();
            assert!(privacy.pending_operation.is_none());
            assert!(privacy.last_completed_operation.is_some());
            let status = reopened.status().await.unwrap();
            assert_eq!(status.phase, "provider_setup_required");
            let exported: Value = reopened.request(Method::POST, "/v1/privacy/export", Some(json!({"kind":"memory_summary","confirmed":true}))).await.unwrap();
            assert_eq!(exported["content"]["exported_count"], 0);
            let bootstrap = reopened.gateway.lock().await.as_ref().unwrap().bootstrap.clone();
            reopened.drain_gateway(&bootstrap).await.unwrap();
            std::fs::write(root.join("acceptance.json"), serde_json::to_vec_pretty(&json!({
                "scope":"isolated native Host and real Python Gateway, no model or UI erasure",
                "erasure":"confirmed", "old_directory_removed":true, "neighbour_preserved":true,
                "replacement_empty":true, "receipt_persisted":true,
            })).unwrap()).unwrap();
        });
    }

    #[test]
    fn quality_commands_reject_unknown_schemas_and_implicit_confirmation() {
        assert!(serde_json::from_value::<QualityInspectRequest>(json!({"kind":"arbitrary_url"})).is_err());
        assert!(serde_json::from_value::<QualityInspectRequest>(json!({"kind":"runs","url":"https://example.com"})).is_err());
        assert!(serde_json::from_value::<QualityResolutionRequest>(json!({"event_id":"event_fixture","resolution":"force","expected_revision":"a".repeat(64),"confirmed":true})).is_err());
        assert!(serde_json::from_value::<QualityResolutionRequest>(json!({"event_id":"event_fixture","resolution":"investigated","expected_revision":"a".repeat(64),"confirmed":"true"})).is_err());
        tauri::async_runtime::block_on(async {
            let state = HostState::new().unwrap();
            // Must reject before attempting to start a Gateway or touching a profile.
            assert!(state.debug_quality_resolve("main", QualityResolutionRequest {event_id:"event_fixture".into(), resolution:QualityResolution::Investigated, expected_revision:"a".repeat(64), confirmed:false}).await.is_err());
            assert!(state.debug_quality_resolve("main", QualityResolutionRequest {event_id:"event_fixture".into(), resolution:QualityResolution::Investigated, expected_revision:"not-a-revision".into(), confirmed:true}).await.is_err());
        });
    }

    #[test]
    fn quarantine_disposition_requires_a_typed_confirmed_revision() {
        assert!(serde_json::from_value::<QuarantineDismissalRequest>(json!({"quarantine_id":"quarantine_fixture","expected_revision":"a".repeat(64),"confirmed":1})).is_err());
        assert!(serde_json::from_value::<QuarantineDismissalRequest>(json!({"quarantine_id":"quarantine_fixture","expected_revision":"a".repeat(64),"confirmed":true,"release":true})).is_err());
        tauri::async_runtime::block_on(async {
            let state = HostState::new().unwrap();
            assert!(state.debug_quarantine_dismiss("main", QuarantineDismissalRequest {quarantine_id:"quarantine_fixture".into(), expected_revision:"a".repeat(64), confirmed:false}).await.is_err());
            assert!(state.debug_quarantine_dismiss("main", QuarantineDismissalRequest {quarantine_id:"quarantine_fixture".into(), expected_revision:"bad".into(), confirmed:true}).await.is_err());
        });
    }

    #[test]
    fn stale_panel_close_cannot_revoke_replacement_session() {
        tauri::async_runtime::block_on(async {
        let state = HostState::new().unwrap();
        *state.debug_session.lock().await = Some(NativeDebugSession {
            info: DebugSessionInfo {schema_version:"DesktopDebugSession.v1".into(), session_id:"debug_new".into(),
                window_label:"main".into(), expires_at_ms:u64::MAX, scopes:vec![]},
            token: Zeroizing::new("fixture-secret".into()), parent_capability_id:"cap_parent".into(),
            base_url:"http://127.0.0.1:1".into(),
        });
        assert_eq!(state.debug_session_close("main", "debug_old").await.unwrap()["closed"], false);
        assert!(state.debug_session.lock().await.is_some());
        assert!(state.debug_session_close("foreign-window", "debug_new").await.is_err());
        assert!(state.debug_session.lock().await.is_some());
        });
    }

    #[test]
    fn gateway_version_conflicts_preserve_safe_details_and_reload_action() {
        let body = br#"{"code":"VERSION_CONFLICT","message":"conversation changed","details":{"current_version":7,"expected_version":6}}"#;
        let shell = ShellError::from(parse_gateway_error(StatusCode::CONFLICT, body));
        assert_eq!(shell.code, "TURN_CONFLICT");
        assert!(!shell.retryable);
        assert_eq!(shell.action, "reload_conversation");
        assert_eq!(shell.safe_details["current_version"], 7);
        assert_eq!(shell.safe_details["expected_version"], 6);
    }
}
