import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import type { ProviderConfigurationView } from "../transport";
import type { WorkloadBinding } from "../contracts/generated";
import { ProviderRoutesEditor } from "./ProviderRoutesEditor";
import { ProviderSettings } from "./ProviderSettings";

const target = (number: number) => ({ provider_profile_id: "provider_fixture", model_id: `model_${number}` });
const binding: WorkloadBinding = { binding_id: "binding_fixture", owner: {}, workload: "response_generation", primary: target(0), fallbacks: [target(1), target(2), target(3), target(4)], requirements: ["chat"], cross_provider_fallback: true, revision: 7 };
const configuration: ProviderConfigurationView = { revision: 10,
  profiles: [{ profile_id: "provider_fixture", owner: {}, display_name: "隔离模型配置", provider_kind: "ollama", protocol: "openai_chat_completions", endpoint: { base_url: "http://127.0.0.1:11434", mode: "local" }, status: "verified", auth_scheme: "none" }],
  models: { provider_fixture: Array.from({ length: 6 }, (_, index) => ({ provider_profile_id: "provider_fixture", model_id: `model_${index}`, capabilities: { chat: true } })) },
  bindings: [binding as unknown as Readonly<Record<string, unknown>>], health: { conversation_ready: true, status: "ready", missing_conversation_workloads: [] },
};
function fixture() {
  const transport = new MockDesktopTransport();
  const bind = vi.spyOn(transport, "bindModelWorkload").mockResolvedValue({ ...binding, revision: 8 });
  const list = vi.spyOn(transport, "listProviders").mockResolvedValue(configuration);
  const remove = vi.spyOn(transport, "deleteProvider").mockResolvedValue();
  return { transport, bind, list, remove };
}

it("preserves every configured fallback in order, requires confirmation and sends the expected revision", async () => {
  const f = fixture(), saved = vi.fn().mockResolvedValue(undefined);
  render(<ProviderRoutesEditor transport={f.transport} configuration={configuration} onSaved={saved} />);
  fireEvent.click(screen.getByRole("button", { name: "修改实际路由" }));
  expect(screen.getByLabelText("降级模型 4")).toHaveValue(JSON.stringify(["provider_fixture", "model_4"]));
  expect(screen.getByRole("button", { name: "确认更新路由" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "我已核对模型用途、主模型和降级顺序" }));
  fireEvent.click(screen.getByRole("button", { name: "确认更新路由" }));
  await screen.findByRole("status");
  expect(f.bind).toHaveBeenCalledExactlyOnceWith({ workload: "response_generation", primary: target(0), fallbacks: [target(1), target(2), target(3), target(4)], requirements: ["chat"], cross_provider_fallback: true, expected_revision: 7 });
  expect(saved).toHaveBeenCalledOnce();
});

it("invalidates confirmation when a target changes and rejects duplicate fallback models before writing", async () => {
  const f = fixture(); render(<ProviderRoutesEditor transport={f.transport} configuration={configuration} onSaved={async () => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "修改实际路由" }));
  const confirm = screen.getByRole("checkbox", { name: "我已核对模型用途、主模型和降级顺序" }); fireEvent.click(confirm);
  fireEvent.change(screen.getByLabelText("降级模型 1"), { target: { value: JSON.stringify(["provider_fixture", "model_0"]) } });
  expect(confirm).not.toBeChecked(); fireEvent.click(confirm); fireEvent.click(screen.getByRole("button", { name: "确认更新路由" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("不能重复"); expect(f.bind).not.toHaveBeenCalled();
});

it("does not claim save success when authoritative refresh fails or automatically retry the write", async () => {
  const f = fixture(); const saved = vi.fn().mockRejectedValue(new Error("refresh-failure"));
  render(<ProviderRoutesEditor transport={f.transport} configuration={configuration} onSaved={saved} />);
  fireEvent.click(screen.getByRole("button", { name: "修改实际路由" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "我已核对模型用途、主模型和降级顺序" })); fireEvent.click(screen.getByRole("button", { name: "确认更新路由" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("未获得完整确认");
  expect(screen.queryByRole("status")).not.toBeInTheDocument(); expect(f.bind).toHaveBeenCalledOnce();
  saved.mockResolvedValue(undefined); fireEvent.click(screen.getByRole("button", { name: "重新读取路由" }));
  expect(await screen.findByRole("status")).toHaveTextContent("没有重发上次修改"); expect(f.bind).toHaveBeenCalledOnce();
});

it("does not show an unread or failed model configuration as no connected models", async () => {
  const f = fixture(); let fail!: (reason: Error) => void;
  f.list.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }));
  render(<ProviderSettings transport={f.transport} onConfigurationChanged={async () => {}} />);
  expect(screen.getByText("正在读取实际模型配置…")).toBeInTheDocument(); expect(screen.queryByText("尚未连接模型")).not.toBeInTheDocument();
  await act(async () => fail(new Error("private-provider-error")));
  expect(await screen.findByRole("alert")).toHaveTextContent("当前连接数量和实际路由未确认"); expect(screen.queryByText("尚未连接模型")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重新读取模型配置" }));
  expect(await screen.findByText("隔离模型配置")).toBeInTheDocument(); expect(f.remove).not.toHaveBeenCalled();
});

it("requires the exact configuration name and reports removal only after refresh and disables duplicate calls", async () => {
  const f = fixture(), changed = vi.fn().mockResolvedValue(undefined);
  let complete!: () => void; f.remove.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
  render(<ProviderSettings transport={f.transport} onConfigurationChanged={changed} />);
  fireEvent.click(await screen.findByRole("button", { name: "移除配置" }));
  const confirm = screen.getByRole("button", { name: "确认移除配置" }); expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText("输入配置名称确认"), { target: { value: "错误名称" } }); expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText("输入配置名称确认"), { target: { value: "隔离模型配置" } });
  fireEvent.click(confirm); fireEvent.click(confirm); expect(f.remove).toHaveBeenCalledExactlyOnceWith("provider_fixture");
  expect(screen.queryByText("配置已移除，实际路由已重新读取。")).not.toBeInTheDocument();
  await act(async () => complete());
  expect(await screen.findByText("配置已移除，实际路由已重新读取。")).toBeInTheDocument(); expect(changed).toHaveBeenCalledOnce();
});

it("clears uncertain delete confirmation, disables stale actions and permits read-only reconciliation", async () => {
  const f = fixture(); f.remove.mockRejectedValue(new Error("private-removal-failure"));
  render(<ProviderSettings transport={f.transport} onConfigurationChanged={async () => {}} />);
  fireEvent.click(await screen.findByRole("button", { name: "移除配置" }));
  fireEvent.change(screen.getByLabelText("输入配置名称确认"), { target: { value: "隔离模型配置" } }); fireEvent.click(screen.getByRole("button", { name: "确认移除配置" }));
  await screen.findByText("删除结果未获完整确认，请重新读取配置后核对，不会自动重试。");
  expect(screen.queryByRole("button", { name: "确认移除配置" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "移除配置" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重新读取模型配置" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "移除配置" })).toBeEnabled()); expect(f.remove).toHaveBeenCalledOnce();
});
