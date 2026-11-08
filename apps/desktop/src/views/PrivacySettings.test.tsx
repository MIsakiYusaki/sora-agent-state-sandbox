import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import type { PrivacyExportReceipt } from "../transport/DesktopTransport";
import { PrivacySettings } from "./PrivacySettings";

function fixtureTransport(profileId = "space-fixture") {
  const transport = new MockDesktopTransport();
  vi.spyOn(transport, "getPrivacyState").mockResolvedValue({ schema_version: "DesktopPrivacyState.v1", profile_id: profileId, space_name: "测试空间", revision: "a".repeat(64), pending_operation: null, last_completed_operation: null });
  return transport;
}

it("passes explicit current-space intent and waits for native save confirmation", async () => {
  const transport = fixtureTransport();
  let complete!: (value: PrivacyExportReceipt) => void;
  const exportData = vi.spyOn(transport, "exportPrivacy").mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  render(<PrivacySettings transport={transport} profileId="space-fixture" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "导出记忆摘要" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "导出记忆摘要" }));
  expect(exportData).toHaveBeenCalledExactlyOnceWith({ kind: "memory_summary", expected_profile_id: "space-fixture", confirmed: true });
  expect(screen.getByRole("button", { name: "导出诊断文件" })).toBeDisabled();
  expect(screen.queryByText(/已保存到/)).not.toBeInTheDocument();
  await act(async () => complete({ schema_version: "DesktopPrivacyExportReceipt.v1", kind: "memory_summary", status: "saved", bytes: 100 }));
  expect(screen.getByText("记忆摘要已保存到你选择的位置。")).toBeInTheDocument();
});

it("reports native cancellation without presenting a saved file", async () => {
  const transport = fixtureTransport();
  vi.spyOn(transport, "exportPrivacy").mockResolvedValue({ schema_version: "DesktopPrivacyExportReceipt.v1", kind: "diagnostic", status: "cancelled" });
  render(<PrivacySettings transport={transport} profileId="space-fixture" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "导出诊断文件" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "导出诊断文件" }));
  expect(await screen.findByText("已取消保存。")).toBeInTheDocument();
  expect(screen.queryByText(/已保存到/)).not.toBeInTheDocument();
});

it("keeps failed or malformed save outcomes unconfirmed and does not repeat automatically", async () => {
  const transport = fixtureTransport();
  const exportData = vi.spyOn(transport, "exportPrivacy").mockResolvedValue({ schema_version: "DesktopPrivacyExportReceipt.v1", kind: "memory_summary", status: "saved", bytes: 0 });
  render(<PrivacySettings transport={transport} profileId="space-fixture" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "导出记忆摘要" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "导出记忆摘要" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("没有获得文件保存成功的确认");
  expect(exportData).toHaveBeenCalledOnce();
});

it("discards a late native result after the current space changes", async () => {
  const transport = fixtureTransport("old-fixture");
  let complete!: (value: PrivacyExportReceipt) => void;
  vi.spyOn(transport, "exportPrivacy").mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  const view = render(<PrivacySettings transport={transport} profileId="old-fixture" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "导出记忆摘要" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "导出记忆摘要" }));
  view.rerender(<PrivacySettings transport={transport} profileId="new-fixture" />);
  await act(async () => complete({ schema_version: "DesktopPrivacyExportReceipt.v1", kind: "memory_summary", status: "saved", bytes: 100 }));
  expect(screen.queryByText(/已保存到/)).not.toBeInTheDocument();
});
