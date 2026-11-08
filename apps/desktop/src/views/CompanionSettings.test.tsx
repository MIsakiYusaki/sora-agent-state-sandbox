import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DesktopTransport } from "../transport";
import type { CompanionGovernanceView, CompanionView } from "../transport/companion";
import { CompanionSettings } from "./CompanionSettings";
import { CompanionGovernance } from "./CompanionGovernance";

afterEach(cleanup);
const baseline: CompanionView = {
  schema_version: "DesktopCompanion.v1",
  persona: { growth_paused: false, global_growth_paused: false, surface_permission: "implicit", revision: "a".repeat(64) },
  relationship: { learning_paused: false, surface_permission: "implicit", revision: "b".repeat(64) },
  agreements: [], active_agreement: { status: "active", revision: "c".repeat(64), user_confirmed: false, comfort_rhythm: null }, repair: null,
};
function transport(methods: Record<string, unknown> = {}) {
  return { getCompanion: vi.fn().mockResolvedValue(baseline), companionAction: vi.fn(), closeDebugSession: vi.fn().mockResolvedValue(undefined), ...methods } as unknown as DesktopTransport;
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes; }); return { promise, resolve }; }

describe("真实陪伴控制的客户端交互", () => {
  it("不把读取失败显示为默认关闭，也不提供写入目标", async () => {
    const api = transport({ getCompanion: vi.fn().mockRejectedValue(new Error("offline")) });
    render(<CompanionSettings transport={api} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("读取失败");
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(api.companionAction).not.toHaveBeenCalled();
  });

  it("核对确认后才发送，实际回执到达前不切换保存状态，连点只发一次", async () => {
    const user = userEvent.setup(); const pending = deferred<CompanionView>();
    const api = transport({ companionAction: vi.fn().mockReturnValue(pending.promise) });
    render(<CompanionSettings transport={api} />);
    const control = await screen.findByRole("switch", { name: "暂停从我们的互动中成长" });
    await user.click(control);
    expect(control).toHaveAttribute("aria-checked", "false");
    expect(api.companionAction).not.toHaveBeenCalled();
    await user.dblClick(screen.getByRole("button", { name: "确认更改" }));
    expect(api.companionAction).toHaveBeenCalledTimes(1);
    expect(api.companionAction).toHaveBeenCalledWith({ action: "controls", payload: { control: "persona_growth", value: true, expected_revision: baseline.persona.revision, confirmed: true } });
    expect(control).toHaveAttribute("aria-checked", "false");
    await act(async () => pending.resolve({ ...baseline, persona: { ...baseline.persona, growth_paused: true } }));
    expect(control).toHaveAttribute("aria-checked", "true");
  });

  it("结果不确定时撤销目标，要求重新读取，不自动重发", async () => {
    const user = userEvent.setup();
    const api = transport({ companionAction: vi.fn().mockRejectedValue(new Error("unknown")) });
    render(<CompanionSettings transport={api} />);
    await user.click(await screen.findByRole("switch", { name: "暂停调整相处方式" }));
    await user.click(screen.getByRole("button", { name: "确认更改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不会自动重发");
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(api.companionAction).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "重新读取陪伴设置" }));
    expect(await screen.findByRole("switch", { name: "暂停调整相处方式" })).toBeInTheDocument();
  });

  it("换空间/传输后，迟到的旧读取不能覆盖新身份", async () => {
    const old = deferred<CompanionView>();
    const a = transport({ getCompanion: vi.fn().mockReturnValue(old.promise) });
    const b = transport({ getCompanion: vi.fn().mockResolvedValue({ ...baseline, persona: { ...baseline.persona, growth_paused: true } }) });
    const view = render(<CompanionSettings transport={a} />);
    view.rerender(<CompanionSettings transport={b} />);
    const control = await screen.findByRole("switch", { name: "暂停从我们的互动中成长" });
    expect(control).toHaveAttribute("aria-checked", "true");
    await act(async () => old.resolve(baseline));
    expect(control).toHaveAttribute("aria-checked", "true");
  });

  it("默认限制不冒充用户确认；拟定约定之后还需核对具体内容", async () => {
    const user = userEvent.setup();
    const row = { id: "agreement-qa", term: "terms.comfort_rhythm", value: "warm_low_pressure", status: "pending_confirmation", expires_at: null, revision: "d".repeat(64), can_confirm: true };
    const api = transport({ companionAction: vi.fn().mockResolvedValue({ ...baseline, agreements: [row] }) });
    render(<CompanionSettings transport={api} />);
    expect(await screen.findByText(/自然相处，尚未确认新的约定/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "撤销当前约定" })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("陪伴节奏"), "warm_low_pressure");
    await user.click(screen.getByRole("button", { name: "拟定约定" }));
    await user.click(await screen.findByRole("button", { name: "核对并确认约定" }));
    expect(api.companionAction).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "确认更改" }));
    expect(api.companionAction).toHaveBeenLastCalledWith({ action: "agreements/confirm", payload: { candidate_id: row.id, expected_revision: row.revision, confirmed: true } });
  });
});

const governance: CompanionGovernanceView = {
  schema_version: "DesktopCompanionGovernance.v1", domain: "persona", truncated: false, duration_ms: 1.2,
  candidates: [], audits: [], contributions: [{ id: "contribution-qa", kind: "contribution", status: "active", target: "warmth", candidate_id: "candidate-qa", created_at: null, source_fingerprints: ["qa-source-fingerprint"], reason: "not_reported", revision: "e".repeat(64) }],
};

describe("真实领域治理", () => {
  it("先开启原生权限、核对影响和勾选确认，再执行精确修订并读取新状态", async () => {
    const user = userEvent.setup();
    const api = transport({ getCompanionGovernance: vi.fn().mockResolvedValueOnce(governance).mockResolvedValue({ ...governance, contributions: [{ ...governance.contributions[0], status: "inactive" }] }),
      openCompanionSession: vi.fn().mockResolvedValue({ schema_version: "DesktopDebugSession.v1", session_id: "session-qa", window_label: "main", expires_at_ms: Date.now() + 300_000, scopes: ["companion.manage"] }),
      resolveCompanionGovernance: vi.fn().mockResolvedValue({ status: "applied", audit_id: "audit-qa", duration_ms: 5 }) });
    render(<CompanionGovernance transport={api} domain="persona" />);
    await user.click(await screen.findByText(/有效 · warmth/));
    const preview = await screen.findByRole("button", { name: "预览撤销此贡献" });
    expect(preview).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "开启5分钟领域管理" }));
    await user.click(preview);
    expect(screen.getByRole("button", { name: "执行这次治理" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "执行这次治理" }));
    await waitFor(() => expect(api.resolveCompanionGovernance).toHaveBeenCalledWith({ domain: "persona", action: "rollback_contribution", target_id: "contribution-qa", expected_revision: "e".repeat(64), confirmed: true }));
    expect(await screen.findByText(/已生效；审计 audit-qa/)).toBeInTheDocument();
    expect(await screen.findByText(/已停用 · warmth/)).toBeInTheDocument();
  });

  it("领域切换后，旧领域迟到的结果不进入新面板", async () => {
    const old = deferred<CompanionGovernanceView>();
    const api = transport({ getCompanionGovernance: vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue({ ...governance, domain: "relationship", contributions: [] }) });
    const view = render(<CompanionGovernance transport={api} domain="persona" />);
    view.rerender(<CompanionGovernance transport={api} domain="relationship" />);
    await screen.findByText("以下为本次实际读取的记录。", { exact: false });
    await act(async () => old.resolve(governance));
    expect(screen.queryByText(/contribution-qa/)).not.toBeInTheDocument();
  });
});
