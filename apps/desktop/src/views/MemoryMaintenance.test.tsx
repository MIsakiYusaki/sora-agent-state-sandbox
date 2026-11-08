import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { DesktopTransport } from "../transport";
import type { MemoryMaintenanceView } from "../transport/maintenance";
import { MemoryMaintenance } from "./MemoryMaintenance";

afterEach(cleanup);
const baseline: MemoryMaintenanceView = { schema_version: "MemoryMaintenance.v1", paused: false, revision: "a".repeat(64), running: true, status: "waiting", next_due_at: null, last_completed_at: null, last_run_id: null, last_result: null, last_error: null, interval_seconds: 3600, batch_limit: 100 };
function api(methods = {}) { return { getMemoryMaintenance: vi.fn().mockResolvedValue(baseline), setMemoryMaintenance: vi.fn(), ...methods } as unknown as DesktopTransport; }

it("展开后读取真实结果，没有记录时不显示虚假的完成数", async () => {
  const user = userEvent.setup(), transport = api(); render(<MemoryMaintenance transport={transport} />);
  expect(transport.getMemoryMaintenance).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: /查看与设置/ }));
  expect(await screen.findByText("尚无已完成的周期整理记录。")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("周期整理已开启");
  expect(screen.queryByText(/最近整理了/)).not.toBeInTheDocument();
});

it("确认前不提交，提交包含精确修订，回执前不虚报暂停", async () => {
  let finish!: (value: MemoryMaintenanceView) => void;
  const user = userEvent.setup(), transport = api({ setMemoryMaintenance: vi.fn(() => new Promise<MemoryMaintenanceView>(resolve => { finish = resolve; })) });
  render(<MemoryMaintenance transport={transport} />);
  await user.click(screen.getByRole("button", { name: /查看与设置/ }));
  await user.click(await screen.findByRole("button", { name: "暂停周期整理" }));
  expect(transport.setMemoryMaintenance).not.toHaveBeenCalled();
  await user.dblClick(screen.getByRole("button", { name: "确认暂停" }));
  expect(transport.setMemoryMaintenance).toHaveBeenCalledExactlyOnceWith({ paused: true, expected_revision: baseline.revision, confirmed: true });
  expect(screen.queryByText("周期整理已暂停")).not.toBeInTheDocument();
  await act(async () => finish({ ...baseline, paused: true, revision: "b".repeat(64) }));
  expect(screen.getByText("周期整理已暂停")).toBeInTheDocument();
});

it("未知更改结果要求重新核对，不自动重发", async () => {
  const user = userEvent.setup(), transport = api({ setMemoryMaintenance: vi.fn().mockRejectedValue(new Error("unknown")) });
  render(<MemoryMaintenance transport={transport} />);
  await user.click(screen.getByRole("button", { name: /查看与设置/ }));
  await user.click(await screen.findByRole("button", { name: "暂停周期整理" }));
  await user.click(screen.getByRole("button", { name: "确认暂停" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("不会自动重发");
  expect(screen.queryByRole("button", { name: "确认暂停" })).not.toBeInTheDocument();
  expect(transport.setMemoryMaintenance).toHaveBeenCalledTimes(1);
});

it("读取错误和服务停止都不冒充正在运行", async () => {
  const user = userEvent.setup(), transport = api({ getMemoryMaintenance: vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ ...baseline, running: false }) });
  render(<MemoryMaintenance transport={transport} />);
  await user.click(screen.getByRole("button", { name: /查看与设置/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("读取失败");
  await user.click(screen.getByRole("button", { name: "重新读取维护状态" }));
  expect(await screen.findByText("周期整理已开启，服务当前未运行")).toBeInTheDocument();
});
