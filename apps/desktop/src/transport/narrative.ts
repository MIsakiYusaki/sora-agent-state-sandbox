export interface NarrativeEvent {
  readonly id: string; readonly event_id: string; readonly revision: number;
  readonly title: string; readonly summary: string; readonly state: string; readonly updated_at: string;
  readonly selected_choice: string | null; readonly choices: ReadonlyArray<{ readonly id: string; readonly label: string }>;
  readonly can_invite: boolean; readonly tasks: ReadonlyArray<{ readonly id: string; readonly title: string; readonly completed: boolean | null }>;
  readonly memory_state: string;
  readonly history: ReadonlyArray<{ readonly id: string; readonly state: string; readonly occurred_at: string }>;
}
export interface NarrativeView {
  readonly schema_version: "DesktopNarrative.v1"; readonly source: "local_preview" | "signed_content";
  readonly available: boolean; readonly world_name: string | null; readonly bundle_id: string | null; readonly canon_version: string | null;
  readonly events: ReadonlyArray<NarrativeEvent>; readonly truncated: boolean; readonly unavailable_reason: string | null;
}
export interface NarrativeActionRequest {
  readonly request_id: string; readonly instance_id: string; readonly expected_revision: number;
  readonly bundle_id: string; readonly canon_version: string; readonly action: "invite" | "choice"; readonly choice_id: string | null;
}
export interface NarrativeActionResult {
  readonly receipt: { readonly schema_version: "WorldOpsUserActionReceipt.v1"; readonly request_id: string; readonly instance_id: string;
    readonly event_id: string; readonly revision: number; readonly state: string; readonly timeline_revision: number; readonly correlation_id: string; readonly llm_called: false };
  readonly view: NarrativeView; readonly duration_ms: number;
}
