import type {
  EndpointMode,
  ProviderKind,
  ProviderProtocol,
} from "../contracts/generated";
import rawCatalog from "./provider-catalog.v1.json";

export type SupportedAuthScheme =
  | "none"
  | "bearer"
  | "anthropic_api_key"
  | "google_api_key";
export type CapabilityKind = "chat" | "embedding" | "rerank";

export interface ProviderModelPreset {
  readonly model_id: string;
  readonly label: string;
  readonly capability_kind: CapabilityKind;
  readonly thinking_control: boolean;
}

export interface ProviderPreset {
  readonly id: string;
  readonly label: string;
  readonly provider_kind: ProviderKind;
  readonly base_url: string;
  readonly endpoint_mode: EndpointMode;
  readonly custom_endpoint: boolean;
  readonly auth_scheme: SupportedAuthScheme;
  readonly protocol: ProviderProtocol;
  readonly models: ReadonlyArray<ProviderModelPreset>;
}

export interface CustomProviderOption {
  readonly provider_kind: ProviderKind;
  readonly label: string;
  readonly protocols: ReadonlyArray<ProviderProtocol>;
  readonly endpoint_modes: ReadonlyArray<EndpointMode>;
  readonly default_auth_scheme: SupportedAuthScheme;
}

export interface ProviderCatalog {
  readonly schema_version: "SoraProviderCatalog.v1";
  readonly catalog_version: string;
  readonly presets: ReadonlyArray<ProviderPreset>;
  readonly custom_provider_options: ReadonlyArray<CustomProviderOption>;
}

export const PROVIDER_CATALOG = validateCatalog(rawCatalog);

function validateCatalog(value: unknown): ProviderCatalog {
  if (!isRecord(value) || value.schema_version !== "SoraProviderCatalog.v1") {
    throw new Error("Provider catalog schema is unsupported");
  }
  if (typeof value.catalog_version !== "string" || !Array.isArray(value.presets)) {
    throw new Error("Provider catalog metadata is invalid");
  }
  if (!Array.isArray(value.custom_provider_options) || value.presets.length === 0) {
    throw new Error("Provider catalog entries are missing");
  }
  return value as unknown as ProviderCatalog;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
