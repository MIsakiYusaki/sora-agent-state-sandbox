import {
  Check,
  CircleAlert,
  KeyRound,
  LoaderCircle,
  Plus,
  RotateCcw,
  ShieldCheck,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import type {
  CreateProfileRequest,
  EndpointMode,
  ModelDescriptor,
  ProviderKind,
  ProviderProfile,
  ProviderProtocol,
  VerifyModelRequest,
} from "../contracts/generated";
import {
  PROVIDER_CATALOG,
  type CapabilityKind,
  type SupportedAuthScheme,
} from "../config/providerCatalog";
import type { DesktopTransport, ProviderConfigurationView } from "../transport";

interface ProviderSettingsProps {
  readonly transport: DesktopTransport;
  readonly onConfigurationChanged: () => Promise<void>;
}

type SetupMode = "preset" | "custom";

const firstPreset = PROVIDER_CATALOG.presets[0];
const firstCustomOption = PROVIDER_CATALOG.custom_provider_options[0];

export function ProviderSettings({ transport, onConfigurationChanged }: ProviderSettingsProps) {
  const [configuration, setConfiguration] = useState<ProviderConfigurationView | null>(null);
  const [mode, setMode] = useState<SetupMode>("preset");
  const [selectedPresetId, setSelectedPresetId] = useState(firstPreset.id);
  const [modelId, setModelId] = useState(firstPreset.models[0].model_id);
  const [customDisplayName, setCustomDisplayName] = useState("");
  const [customKind, setCustomKind] = useState<ProviderKind>(firstCustomOption.provider_kind);
  const [customBaseUrl, setCustomBaseUrl] = useState("");
  const [customProtocol, setCustomProtocol] = useState<ProviderProtocol>(firstCustomOption.protocols[0]);
  const [endpointMode, setEndpointMode] = useState<EndpointMode>(firstCustomOption.endpoint_modes[0]);
  const [authScheme, setAuthScheme] = useState<SupportedAuthScheme>(firstCustomOption.default_auth_scheme);
  const [capabilityKind, setCapabilityKind] = useState<CapabilityKind>("chat");
  const [thinkingControl, setThinkingControl] = useState(false);
  const [embeddingDimension, setEmbeddingDimension] = useState("");
  const [editingProfile, setEditingProfile] = useState<ProviderProfile | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const selectedPreset = useMemo(
    () => PROVIDER_CATALOG.presets.find((preset) => preset.id === selectedPresetId) ?? firstPreset,
    [selectedPresetId],
  );
  const selectedCustomOption = useMemo(
    () => PROVIDER_CATALOG.custom_provider_options.find((item) => item.provider_kind === customKind) ?? firstCustomOption,
    [customKind],
  );
  const selectedPresetModel = selectedPreset.models.find((model) => model.model_id === modelId)
    ?? selectedPreset.models[0];

  const refresh = async () => {
    setConfiguration(await transport.listProviders());
  };

  useEffect(() => {
    void refresh().catch((cause) => setError(errorMessage(cause)));
  }, []);

  const createOrResumeProvider = async (event: FormEvent) => {
    event.preventDefault();
    setBusy("create");
    setError(null);
    setNotice(null);
    try {
      const draft = providerDraft();
      const profile = editingProfile ?? await transport.createProvider(draft.profile);
      const outcome = await transport.promptAndVerifyCredential({
        schema_version: "DesktopProviderCredentialPromptRequest.v1",
        profile_id: profile.profile_id,
        verification: draft.verification,
      });
      setNotice(
        outcome.routing.ready_for_conversation
          ? "模型已连接，对话链路已经就绪。"
          : `模型已验证，仍有 ${outcome.routing.missing_conversation_workloads?.length ?? 0} 条运行路径需要其他模型。`,
      );
      setEditingProfile(null);
      await refresh();
      await onConfigurationChanged();
    } catch (cause) {
      setError(errorMessage(cause));
      await refresh().catch(() => undefined);
    } finally {
      setBusy(null);
    }
  };

  const rotate = async (profile: ProviderProfile, model: ModelDescriptor) => {
    setBusy(profile.profile_id);
    setError(null);
    setNotice(null);
    try {
      const outcome = await transport.rotateCredential({
        schema_version: "DesktopProviderRotatePromptRequest.v1",
        profile_id: profile.profile_id,
        verification: verificationFromModel(model),
      });
      setNotice(
        outcome.routing.ready_for_conversation
          ? "密钥已安全轮换。"
          : "密钥已轮换，部分运行路径仍需其他模型。",
      );
      await refresh();
      await onConfigurationChanged();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(null);
    }
  };

  const resume = (profile: ProviderProfile) => {
    const knownModel = configuration?.models[profile.profile_id]?.[0];
    setMode("custom");
    setEditingProfile(profile);
    setCustomDisplayName(profile.display_name);
    setCustomKind(profile.provider_kind);
    setCustomBaseUrl(profile.endpoint.base_url);
    setCustomProtocol(profile.protocol);
    setEndpointMode(profile.endpoint.mode ?? "remote");
    setAuthScheme(normalizeAuthScheme(profile.auth_scheme));
    setModelId(knownModel?.model_id ?? "");
    setCapabilityKind(capabilityFromModel(knownModel));
    setThinkingControl(knownModel?.capabilities.thinking_control ?? false);
    setEmbeddingDimension(String(knownModel?.capabilities.embedding_dimension ?? ""));
    setNotice(null);
    setError(null);
  };

  const providerDraft = (): { profile: CreateProfileRequest; verification: VerifyModelRequest } => {
    if (mode === "preset" && !editingProfile) {
      return {
        profile: {
          display_name: selectedPreset.label,
          provider_kind: selectedPreset.provider_kind,
          protocol: selectedPreset.protocol,
          base_url: selectedPreset.base_url,
          endpoint_mode: selectedPreset.endpoint_mode,
          custom_endpoint: selectedPreset.custom_endpoint,
          auth_scheme: selectedPreset.auth_scheme,
        },
        verification: {
          model_id: modelId.trim(),
          capability_kind: selectedPresetModel.capability_kind,
          thinking_control: selectedPresetModel.thinking_control,
        },
      };
    }
    const dimension = Number.parseInt(embeddingDimension, 10);
    return {
      profile: {
        display_name: customDisplayName.trim(),
        provider_kind: customKind,
        protocol: customProtocol,
        base_url: customBaseUrl.trim(),
        endpoint_mode: endpointMode,
        custom_endpoint: true,
        auth_scheme: authScheme,
      },
      verification: {
        model_id: modelId.trim(),
        capability_kind: capabilityKind,
        thinking_control: capabilityKind === "chat" && thinkingControl,
        embedding_dimension:
          capabilityKind === "embedding" && Number.isFinite(dimension) && dimension > 0
            ? dimension
            : null,
      },
    };
  };

  const customValid = Boolean(editingProfile || (customDisplayName.trim() && customBaseUrl.trim()));
  const canSubmit = Boolean(modelId.trim() && (mode === "preset" || customValid));

  return (
    <section className="settings-section" aria-labelledby="provider-heading">
      <div className="settings-heading">
        <div>
          <h2 id="provider-heading">模型与连接</h2>
          <p>当前对话与后台任务使用的模型来源</p>
        </div>
        <span className="security-badge"><ShieldCheck size={15} /> 本机安全存储</span>
      </div>

      <div className="provider-list">
        {configuration?.profiles.map((profile) => {
          const models = configuration.models[profile.profile_id] ?? [];
          const verifiedModel = models[0];
          const canRotate = Boolean(profile.auth_scheme !== "none" && profile.status === "verified" && verifiedModel);
          return (
            <div className="provider-row" key={profile.profile_id}>
              <div className="provider-mark" aria-hidden="true">
                {profile.display_name.slice(0, 1).toUpperCase()}
              </div>
              <div className="provider-row__content">
                <strong>{profile.display_name}</strong>
                <span>{verifiedModel?.model_id ?? profile.endpoint.base_url}</span>
              </div>
              <span className={`status-label status-label--${profile.status}`}>
                {profile.status === "verified" ? <Check size={13} /> : null}
                {statusLabel(profile.status)}
              </span>
              {profile.auth_scheme === "none" && profile.status === "verified" ? (
                <span className="provider-action-spacer" />
              ) : (
                <button
                  type="button"
                  className="icon-button"
                  title={canRotate ? "轮换密钥" : "继续配置"}
                  aria-label={canRotate ? `轮换 ${profile.display_name} 密钥` : `继续配置 ${profile.display_name}`}
                  disabled={busy !== null}
                  onClick={() => canRotate && verifiedModel ? void rotate(profile, verifiedModel) : resume(profile)}
                >
                  {busy === profile.profile_id
                    ? <LoaderCircle size={17} className="spin" />
                    : canRotate ? <RotateCcw size={17} /> : <Wrench size={17} />}
                </button>
              )}
            </div>
          );
        })}
        {!configuration?.profiles.length && (
          <div className="provider-empty">
            <KeyRound size={20} />
            <span>尚未连接模型</span>
          </div>
        )}
      </div>

      {configuration && configuration.profiles.length > 0 && !configuration.health.conversation_ready && (
        <div className="setup-warning" role="status">
          <CircleAlert size={16} />
          <span>当前模型组合尚不能完整运行文字对话。</span>
        </div>
      )}

      <form className="provider-form" onSubmit={createOrResumeProvider}>
        <div className="segmented-control" aria-label="连接方式">
          <button
            type="button"
            className={mode === "preset" ? "segmented-control__item is-active" : "segmented-control__item"}
            onClick={() => { setMode("preset"); setEditingProfile(null); setModelId(firstPreset.models[0].model_id); }}
          >
            内置服务
          </button>
          <button
            type="button"
            className={mode === "custom" ? "segmented-control__item is-active" : "segmented-control__item"}
            onClick={() => { setMode("custom"); setEditingProfile(null); setModelId(""); }}
          >
            自定义
          </button>
        </div>

        {mode === "preset" && !editingProfile ? (
          <div className="field-row">
            <label>
              <span>供应商</span>
              <select
                value={selectedPresetId}
                onChange={(event) => {
                  const preset = PROVIDER_CATALOG.presets.find((item) => item.id === event.target.value) ?? firstPreset;
                  setSelectedPresetId(preset.id);
                  setModelId(preset.models[0].model_id);
                }}
              >
                {PROVIDER_CATALOG.presets.map((preset) => (
                  <option key={preset.id} value={preset.id}>{preset.label}</option>
                ))}
              </select>
            </label>
            <label>
              <span>模型 ID</span>
              <select value={modelId} onChange={(event) => setModelId(event.target.value)}>
                {selectedPreset.models.map((model) => (
                  <option key={model.model_id} value={model.model_id}>{model.label}</option>
                ))}
              </select>
            </label>
          </div>
        ) : (
          <CustomProviderFields
            locked={editingProfile !== null}
            displayName={customDisplayName}
            providerKind={customKind}
            baseUrl={customBaseUrl}
            protocol={customProtocol}
            endpointMode={endpointMode}
            authScheme={authScheme}
            modelId={modelId}
            capabilityKind={capabilityKind}
            thinkingControl={thinkingControl}
            embeddingDimension={embeddingDimension}
            onDisplayName={setCustomDisplayName}
            onBaseUrl={setCustomBaseUrl}
            onModelId={setModelId}
            onCapabilityKind={setCapabilityKind}
            onThinkingControl={setThinkingControl}
            onEmbeddingDimension={setEmbeddingDimension}
            onAuthScheme={setAuthScheme}
            onEndpointMode={setEndpointMode}
            onProviderKind={(kind) => {
              const option = PROVIDER_CATALOG.custom_provider_options.find((item) => item.provider_kind === kind) ?? firstCustomOption;
              setCustomKind(option.provider_kind);
              setCustomProtocol(option.protocols[0]);
              setEndpointMode(option.endpoint_modes[0]);
              setAuthScheme(option.default_auth_scheme);
              setCapabilityKind(capabilityFromProtocol(option.protocols[0]));
            }}
            onProtocol={(protocol) => {
              setCustomProtocol(protocol);
              setCapabilityKind(capabilityFromProtocol(protocol));
            }}
            option={selectedCustomOption}
          />
        )}

        <div className="provider-form__actions">
          <button className="command-button" type="submit" disabled={busy !== null || !canSubmit}>
            {busy === "create" ? <LoaderCircle size={17} className="spin" /> : <Plus size={17} />}
            {editingProfile ? "继续验证" : "连接模型"}
          </button>
          {editingProfile && (
            <button className="text-button" type="button" onClick={() => setEditingProfile(null)}>
              <X size={15} /> 取消
            </button>
          )}
        </div>
      </form>
      <p className="native-prompt-note">
        {authScheme === "none" && mode === "custom"
          ? "此连接不需要密钥。"
          : "密钥由 Windows 原生安全窗口录入，不会进入此页面、日志或诊断包。"}
      </p>
      {notice && <div className="inline-notice" role="status">{notice}</div>}
      {error && <div className="inline-error" role="alert">{error}</div>}
    </section>
  );
}

interface CustomProviderFieldsProps {
  readonly locked: boolean;
  readonly displayName: string;
  readonly providerKind: ProviderKind;
  readonly baseUrl: string;
  readonly protocol: ProviderProtocol;
  readonly endpointMode: EndpointMode;
  readonly authScheme: SupportedAuthScheme;
  readonly modelId: string;
  readonly capabilityKind: CapabilityKind;
  readonly thinkingControl: boolean;
  readonly embeddingDimension: string;
  readonly option: (typeof PROVIDER_CATALOG.custom_provider_options)[number];
  readonly onDisplayName: (value: string) => void;
  readonly onProviderKind: (value: ProviderKind) => void;
  readonly onBaseUrl: (value: string) => void;
  readonly onProtocol: (value: ProviderProtocol) => void;
  readonly onEndpointMode: (value: EndpointMode) => void;
  readonly onAuthScheme: (value: SupportedAuthScheme) => void;
  readonly onModelId: (value: string) => void;
  readonly onCapabilityKind: (value: CapabilityKind) => void;
  readonly onThinkingControl: (value: boolean) => void;
  readonly onEmbeddingDimension: (value: string) => void;
}

function CustomProviderFields(props: CustomProviderFieldsProps) {
  return (
    <div className="custom-provider-grid">
      <label>
        <span>连接名称</span>
        <input value={props.displayName} disabled={props.locked} onChange={(event) => props.onDisplayName(event.target.value)} />
      </label>
      <label>
        <span>服务类型</span>
        <select value={props.providerKind} disabled={props.locked} onChange={(event) => props.onProviderKind(event.target.value as ProviderKind)}>
          {PROVIDER_CATALOG.custom_provider_options.map((item) => (
            <option key={item.provider_kind} value={item.provider_kind}>{item.label}</option>
          ))}
        </select>
      </label>
      <label className="field-span-2">
        <span>服务地址</span>
        <input type="url" value={props.baseUrl} disabled={props.locked} placeholder="https://api.example.com/v1" onChange={(event) => props.onBaseUrl(event.target.value)} />
      </label>
      <label>
        <span>协议</span>
        <select value={props.protocol} disabled={props.locked} onChange={(event) => props.onProtocol(event.target.value as ProviderProtocol)}>
          {props.option.protocols.map((protocol) => <option key={protocol} value={protocol}>{protocolLabel(protocol)}</option>)}
        </select>
      </label>
      <label>
        <span>连接位置</span>
        <select value={props.endpointMode} disabled={props.locked} onChange={(event) => props.onEndpointMode(event.target.value as EndpointMode)}>
          {props.option.endpoint_modes.map((value) => <option key={value} value={value}>{value === "local" ? "本机" : "远程"}</option>)}
        </select>
      </label>
      <label>
        <span>模型 ID</span>
        <input value={props.modelId} onChange={(event) => props.onModelId(event.target.value)} />
      </label>
      <label>
        <span>模型用途</span>
        <select value={props.capabilityKind} onChange={(event) => props.onCapabilityKind(event.target.value as CapabilityKind)}>
          <option value="chat">对话</option>
          <option value="embedding">向量</option>
          <option value="rerank">重排</option>
        </select>
      </label>
      <label>
        <span>鉴权方式</span>
        <select value={props.authScheme} disabled={props.locked} onChange={(event) => props.onAuthScheme(event.target.value as SupportedAuthScheme)}>
          <option value="bearer">API Key</option>
          <option value="anthropic_api_key">Anthropic Key</option>
          <option value="google_api_key">Google Key</option>
          <option value="none">无需密钥</option>
        </select>
      </label>
      {props.capabilityKind === "embedding" && (
        <label>
          <span>向量维度（可选）</span>
          <input inputMode="numeric" value={props.embeddingDimension} onChange={(event) => props.onEmbeddingDimension(event.target.value.replace(/\D/g, ""))} />
        </label>
      )}
      {props.capabilityKind === "chat" && (
        <label className="checkbox-field">
          <input type="checkbox" checked={props.thinkingControl} onChange={(event) => props.onThinkingControl(event.target.checked)} />
          <span>支持关闭 Thinking</span>
        </label>
      )}
    </div>
  );
}

function verificationFromModel(model: ModelDescriptor): VerifyModelRequest {
  return {
    model_id: model.model_id,
    capability_kind: capabilityFromModel(model),
    thinking_control: model.capabilities.thinking_control ?? false,
    embedding_dimension: model.capabilities.embedding_dimension ?? null,
    context_window: model.capabilities.context_window ?? null,
    max_output_tokens: model.capabilities.max_output_tokens ?? null,
  };
}

function capabilityFromModel(model?: ModelDescriptor): CapabilityKind {
  if (model?.capabilities.embedding) return "embedding";
  if (model?.capabilities.rerank) return "rerank";
  return "chat";
}

function capabilityFromProtocol(protocol: ProviderProtocol): CapabilityKind {
  if (protocol === "openai_embeddings") return "embedding";
  if (protocol === "http_rerank") return "rerank";
  return "chat";
}

function normalizeAuthScheme(value?: string): SupportedAuthScheme {
  return value === "none" || value === "anthropic_api_key" || value === "google_api_key"
    ? value
    : "bearer";
}

function protocolLabel(protocol: ProviderProtocol): string {
  const labels: Record<ProviderProtocol, string> = {
    openai_chat_completions: "Chat Completions",
    openai_responses: "Responses API",
    anthropic_messages: "Anthropic Messages",
    gemini_native: "Gemini Native",
    openai_embeddings: "Embeddings",
    http_rerank: "HTTP Rerank",
  };
  return labels[protocol];
}

function statusLabel(status: ProviderProfile["status"]): string {
  const labels: Record<NonNullable<ProviderProfile["status"]>, string> = {
    draft: "待录入",
    credential_bound: "待验证",
    verifying: "验证中",
    verified: "可用",
    degraded: "异常",
    disabled: "已停用",
    deleted: "已删除",
  };
  return status ? labels[status] : "未知";
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "操作失败，请稍后重试";
}
