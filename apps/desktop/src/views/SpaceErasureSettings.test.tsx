import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import type { PrivacyState } from "../transport/DesktopTransport";
import { SpaceErasureSettings } from "./SpaceErasureSettings";

const ready: PrivacyState = { schema_version: "DesktopPrivacyState.v1", profile_id: "fixture-space", space_name: "隔离空间", revision: "a".repeat(64), pending_operation: null, last_completed_operation: null };

it("requires the full phrase and explicit consent and never claims deletion from an IPC acknowledgement", async () => {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getPrivacyState").mockResolvedValue(ready);
  const erase = vi.spyOn(transport, "eraseSpace").mockResolvedValue();
  render(<SpaceErasureSettings transport={transport} profileId={ready.profile_id} disabled={false} onBlockedChange={vi.fn()} />);
  const button = await screen.findByRole("button", { name: "确认删除并重启" });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "删除当前空间" } });
  expect(button).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button);
  await waitFor(() => expect(erase).toHaveBeenCalledExactlyOnceWith({ expected_profile_id: ready.profile_id, expected_revision: ready.revision, confirmed: true, confirmation: "删除当前空间" }));
  expect(await screen.findByText(/正在重新启动客户端/)).toBeInTheDocument();
  expect(screen.queryByText("上次空间删除已完成。")).not.toBeInTheDocument();
});

it("re-reads uncertain outcomes and resumes the recorded operation without automatic retries", async () => {
  const transport = new MockDesktopTransport();
  const read = vi.spyOn(transport, "getPrivacyState").mockResolvedValue(ready);
  const erase = vi.spyOn(transport, "eraseSpace").mockRejectedValue(new Error("lost receipt"));
  render(<SpaceErasureSettings transport={transport} profileId={ready.profile_id} disabled={false} onBlockedChange={vi.fn()} />);
  await screen.findByRole("button", { name: "确认删除并重启" });
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "删除当前空间" } });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "确认删除并重启" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("删除未获得完整确认");
  expect(erase).toHaveBeenCalledOnce();
  read.mockResolvedValue({ ...ready, revision: "b".repeat(64), pending_operation: "erase_" + "1".repeat(32) });
  fireEvent.click(screen.getByRole("button", { name: "重新读取删除状态" }));
  expect(await screen.findByRole("button", { name: "继续恢复删除" })).toBeDisabled();
  expect(screen.getByRole("textbox")).toHaveValue("");
  expect(erase).toHaveBeenCalledOnce();
});

it("rejects a foreign or malformed state and exposes only the last completed receipt", async () => {
  const transport = new MockDesktopTransport();
  const read = vi.spyOn(transport, "getPrivacyState").mockResolvedValue({ ...ready, profile_id: "foreign" });
  const blocked = vi.fn();
  render(<SpaceErasureSettings transport={transport} profileId={ready.profile_id} disabled={false} onBlockedChange={blocked} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("空间删除状态读取失败");
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(blocked).not.toHaveBeenCalledWith(false);
  read.mockResolvedValue({ ...ready, last_completed_operation: "erase_" + "2".repeat(32) });
  fireEvent.click(screen.getByRole("button", { name: "重新读取删除状态" }));
  expect(await screen.findByText("上次空间删除已完成。")).toBeInTheDocument();
});

it("ignores stale reads after unmount or a space switch", async () => {
  const transport = new MockDesktopTransport();
  let complete!: (value: PrivacyState) => void;
  vi.spyOn(transport, "getPrivacyState").mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  const blocked = vi.fn();
  const view = render(<SpaceErasureSettings transport={transport} profileId={ready.profile_id} disabled={false} onBlockedChange={blocked} />);
  view.unmount();
  await act(async () => complete(ready));
  expect(blocked).not.toHaveBeenCalledWith(false);
});
