export type SpaceId =
  | "conversation"
  | "history"
  | "memory"
  | "controls"
  | "settings"
  | "developer";

export type PrototypeState =
  | "daily"
  | "first-run"
  | "empty"
  | "no-provider"
  | "offline"
  | "recovering"
  | "version-conflict"
  | "update-ready"
  | "safe-mode";
