export interface MemoryMaintenanceView {
  readonly schema_version: "MemoryMaintenance.v1";
  readonly paused: boolean; readonly revision: string; readonly running: boolean; readonly status: string;
  readonly next_due_at: string | null; readonly last_completed_at: string | null; readonly last_run_id: string | null;
  readonly last_result: Readonly<Record<string, number>> | null; readonly last_error: string | null;
  readonly interval_seconds: number; readonly batch_limit: number;
}
export interface MemoryMaintenanceRequest {
  readonly paused: boolean; readonly expected_revision: string; readonly confirmed: true;
}
