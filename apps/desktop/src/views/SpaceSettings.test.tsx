import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import { SpaceSettings } from "./SpaceSettings";
import type { SpacePage } from "../transport/DesktopTransport";

const initial: SpacePage = { revision: "r1", active_id: "local", spaces: [{ id: "local", name: "原空间" }, { id: "space_other", name: "另一个空间" }] };
function fixture() {
  const transport = new MockDesktopTransport();
  const list = vi.spyOn(transport, "listSpaces").mockResolvedValue(initial);
  const create = vi.spyOn(transport, "createSpace").mockResolvedValue({ ...initial, revision: "r2", spaces: [...initial.spaces, { id: "space_new", name: "新增测试" }] });
  const change = vi.spyOn(transport, "switchSpace").mockResolvedValue();
  const open = vi.spyOn(transport, "openSpaceLocation").mockResolvedValue();
  return { transport, list, create, change, open };
}
it("creates a real catalog entry without switching or copying the current profile", async () => {
  const f = fixture(); render(<SpaceSettings transport={f.transport} profileId="local" />);
  const name = await screen.findByLabelText("新空间名称");
  fireEvent.change(name, { target: { value: "  新增测试  " } });
  fireEvent.click(screen.getByRole("button", { name: "创建独立空间" }));
  expect(await screen.findByText(/独立空间已创建/)).toBeInTheDocument();
  expect(f.create).toHaveBeenCalledExactlyOnceWith("新增测试", "r1");
  expect(f.change).not.toHaveBeenCalled(); expect(screen.getByText("当前数据空间：local · 原空间")).toBeInTheDocument();
});
it("requires a selected space and explicit restart confirmation and sends the catalog revision", async () => {
  const f = fixture(); render(<SpaceSettings transport={f.transport} profileId="local" />);
  const select = await screen.findByLabelText("目标独立空间");
  const confirm = screen.getByRole("button", { name: "确认切换并重启" });
  expect(confirm).toBeDisabled();
  fireEvent.change(select, { target: { value: "space_other" } }); expect(confirm).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(confirm); fireEvent.click(confirm);
  expect(await screen.findByText("正在重新启动客户端以载入新空间…")).toBeInTheDocument();
  expect(f.change).toHaveBeenCalledExactlyOnceWith({ space_id: "space_other", expected_revision: "r1", confirmed: true });
});
it("clears confirmation on target changes and disables switching during a turn", async () => {
  const f = fixture(); const view = render(<SpaceSettings transport={f.transport} profileId="local" />);
  const select = await screen.findByLabelText("目标独立空间");
  fireEvent.change(select, { target: { value: "space_other" } }); fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(select, { target: { value: "" } }); expect(screen.getByRole("checkbox")).not.toBeChecked();
  view.rerender(<SpaceSettings transport={f.transport} profileId="local" switchingAllowed={false} />);
  expect(screen.getByLabelText("目标独立空间")).toBeDisabled(); expect(f.change).not.toHaveBeenCalled();
});
it("does not show a failed read or foreign profile as an empty directory", async () => {
  const f = fixture(); f.list.mockResolvedValue({ ...initial, active_id: "foreign" });
  render(<SpaceSettings transport={f.transport} profileId="local" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("与当前身份不一致");
  expect(screen.queryByLabelText("新空间名称")).not.toBeInTheDocument();
  f.list.mockResolvedValue(initial); fireEvent.click(screen.getByRole("button", { name: "重新读取空间目录" }));
  expect(await screen.findByLabelText("新空间名称")).toBeInTheDocument();
});
it("reconciles an uncertain creation by reading only and never retries automatically", async () => {
  const f = fixture(); f.create.mockRejectedValue(new Error("private-file-error"));
  render(<SpaceSettings transport={f.transport} profileId="local" />);
  fireEvent.change(await screen.findByLabelText("新空间名称"), { target: { value: "新增测试" } }); fireEvent.click(screen.getByRole("button", { name: "创建独立空间" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("创建结果未获完整确认");
  expect(screen.queryByLabelText("新空间名称")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重新读取空间目录" }));
  await screen.findByLabelText("新空间名称"); expect(f.create).toHaveBeenCalledOnce();
});
it("discards a previous profile's late directory and only opens a Host-selected location", async () => {
  const f = fixture(); let complete!: (page: SpacePage) => void;
  f.list.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; })).mockResolvedValue({ revision:"r3", active_id:"another", spaces:[{ id:"another", name:"新身份" }] });
  const view = render(<SpaceSettings transport={f.transport} profileId="local" />);
  view.rerender(<SpaceSettings transport={f.transport} profileId="another" />);
  await waitFor(() => expect(screen.getByText("当前数据空间：another · 新身份")).toBeInTheDocument());
  await act(async () => complete(initial)); expect(screen.queryByText(/原空间/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "打开数据位置" }));
  await screen.findByText("已请求 Windows 打开当前空间的数据位置。"); expect(f.open).toHaveBeenCalledExactlyOnceWith();
});
