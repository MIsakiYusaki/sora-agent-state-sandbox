export type SurfacePermission = "silent" | "implicit" | "explicit";
export type ComfortRhythm = "gentle_low_assumption" | "warm_low_pressure" | "warm_flexible";
export interface CompanionView {
  readonly schema_version: "DesktopCompanion.v1";
  readonly persona: { readonly growth_paused: boolean; readonly global_growth_paused: boolean; readonly surface_permission: SurfacePermission; readonly revision: string };
  readonly relationship: { readonly learning_paused: boolean; readonly surface_permission: SurfacePermission; readonly revision: string };
  readonly agreements: ReadonlyArray<{ readonly id: string; readonly term: string | null; readonly value: string | null; readonly status: string; readonly expires_at: string | null; readonly revision: string; readonly can_confirm: boolean }>;
  readonly active_agreement: { readonly status: string; readonly revision: string; readonly user_confirmed: boolean; readonly comfort_rhythm: ComfortRhythm | null };
  readonly repair: { readonly id: string; readonly status: string; readonly allowed_transitions: ReadonlyArray<string> } | null;
}
export type CompanionActionRequest =
  | { readonly action: "controls"; readonly payload: { readonly control: "persona_growth" | "relationship_learning"; readonly value: boolean; readonly expected_revision: string; readonly confirmed: true } }
  | { readonly action: "controls"; readonly payload: { readonly control: "persona_surface" | "relationship_surface"; readonly value: SurfacePermission; readonly expected_revision: string; readonly confirmed: true } }
  | { readonly action: "agreements/propose"; readonly payload: { readonly rhythm: ComfortRhythm } }
  | { readonly action: "agreements/confirm"; readonly payload: { readonly candidate_id: string; readonly expected_revision: string; readonly confirmed: true } }
  | { readonly action: "agreements/revoke"; readonly payload: { readonly expected_revision: string; readonly confirmed: true } }
  | { readonly action: "repair"; readonly payload: { readonly repair_id: string; readonly expected_status: string; readonly to_state: "acknowledged" | "cooling_off" | "repair_in_progress" | "repaired" | "accepted_difference" | "unresolved"; readonly confirmed: true } };
export interface CompanionGovernanceRow {
  readonly id: string; readonly kind: "candidate" | "contribution"; readonly status: string;
  readonly target: string; readonly created_at: string | null; readonly candidate_id: string | null;
  readonly source_fingerprints: ReadonlyArray<string>; readonly reason: string; readonly revision: string;
}
export interface CompanionGovernanceView {
  readonly schema_version: "DesktopCompanionGovernance.v1"; readonly domain: "persona" | "relationship";
  readonly candidates: ReadonlyArray<CompanionGovernanceRow>; readonly contributions: ReadonlyArray<CompanionGovernanceRow>;
  readonly audits: ReadonlyArray<{ readonly id: string; readonly action: string; readonly entity_id: string; readonly created_at: string | null }>;
  readonly truncated: boolean; readonly duration_ms: number;
}
export interface CompanionGovernanceDecision {
  readonly domain: "persona" | "relationship"; readonly action: "reject_candidate" | "rollback_contribution";
  readonly target_id: string; readonly expected_revision: string; readonly confirmed: true;
}
export interface CompanionGovernanceReceipt {
  readonly status: "applied"; readonly target_id: string; readonly action: string; readonly audit_id: string; readonly duration_ms: number;
}
