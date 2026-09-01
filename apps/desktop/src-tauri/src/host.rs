use std::{
    env,
    ffi::OsString,
    fs::File,
    io::{self, BufRead, BufReader, Read},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    thread::{self, JoinHandle},
    time::Duration,
};

use futures_util::StreamExt;
use reqwest::{Method, StatusCode};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter};
use thiserror::Error;
use tokio::sync::Mutex;
use url::Url;
use uuid::Uuid;
use zeroize::Zeroizing;

pub type HostResult<T> = Result<T, HostError>;

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
            HostError::InvalidBootstrap | HostError::InvalidResponse => {
                ("INTERNAL_ERROR", true, "restart_gateway")
            }
        };
        let safe_details = match &error {
            HostError::ConversationVersionConflict { safe_details, .. } => safe_details.clone(),
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

pub struct HostState {
    gateway: Mutex<Option<GatewayRuntime>>,
    http: reqwest::Client,
    profile_id: String,
    subject_id: String,
    agent_id: String,
    client_instance_id: String,
}

impl HostState {
    pub fn new() -> HostResult<Self> {
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(90))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| HostError::GatewayUnavailable)?;
        Ok(Self {
            gateway: Mutex::new(None),
            http,
            profile_id: env::var("SORA_PROFILE_ID").unwrap_or_else(|_| "local".into()),
            subject_id: env::var("SORA_SUBJECT_ID").unwrap_or_else(|_| "local-user".into()),
            agent_id: env::var("SORA_AGENT_ID").unwrap_or_else(|_| "sora".into()),
            client_instance_id: format!("desktop_{}", Uuid::new_v4().simple()),
        })
    }

    async fn bootstrap(&self) -> HostResult<GatewayBootstrap> {
        let mut guard = self.gateway.lock().await;
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

    pub async fn status(&self) -> HostResult<DesktopStatus> {
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

    pub async fn conversations(&self) -> HostResult<Value> {
        self.request(Method::GET, "/v1/conversations?limit=100", None)
            .await
    }

    pub async fn memories(&self) -> HostResult<Value> {
        self.request(Method::GET, "/v1/memories", None).await
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
        tokio::spawn(async move {
            if let Err(error) =
                stream_events(http, bootstrap, app.clone(), &accepted_for_stream).await
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
                if matches!(
                    event.event_type.as_str(),
                    "turn.committed" | "turn.cancelled" | "turn.failed"
                ) {
                    return Ok(());
                }
            }
        }
    }
    Ok(())
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
