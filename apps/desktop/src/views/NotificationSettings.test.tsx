import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { NotificationSettings } from "./NotificationSettings";
import { MockDesktopTransport } from "../transport/MockDesktopTransport";
import type { NotificationSettingsView } from "../transport/DesktopTransport";

const initial: NotificationSettingsView = { revision:"r1", os_permission:"enabled", preferences:{ enabled:false,quiet_enabled:true,quiet_start:1320,quiet_end:480 } };
function fixture() {
  const transport=new MockDesktopTransport();
  const read=vi.spyOn(transport,"getNotificationSettings").mockResolvedValue(initial);
  const save=vi.spyOn(transport,"saveNotificationSettings").mockImplementation(async preferences=>({...initial,revision:"r2",preferences}));
  const test=vi.spyOn(transport,"testNotification").mockResolvedValue({status:"submitted_to_windows"});
  return {transport,read,save,test};
}
it("uses explicit save, keeps last confirmed policy until receipt and passes expected revision",async()=>{
  const f=fixture();render(<NotificationSettings transport={f.transport} profileId="local"/>);
  fireEvent.click(await screen.findByRole("checkbox",{name:"允许在后台完成回应时提示我"}));
  expect(f.save).not.toHaveBeenCalled();expect(screen.getByText(/修改尚未保存/)).toBeInTheDocument();
  expect(screen.getByRole("button",{name:"发送测试通知"})).toBeDisabled();
  fireEvent.click(screen.getByRole("button",{name:"保存通知偏好"}));
  expect(await screen.findByText(/通知偏好已保存/)).toBeInTheDocument();
  expect(f.save).toHaveBeenCalledExactlyOnceWith({...initial.preferences,enabled:true},"r1");
});
it("does not fabricate saved preferences after failure and reconciles by reading without mutation",async()=>{
  const f=fixture();f.save.mockRejectedValue(new Error("private-file-error"));
  render(<NotificationSettings transport={f.transport} profileId="local"/>);
  fireEvent.click(await screen.findByRole("checkbox",{name:"允许在后台完成回应时提示我"}));fireEvent.click(screen.getByRole("button",{name:"保存通知偏好"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("保存结果未获得完整确认");
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"重新读取通知设置"}));await screen.findByRole("checkbox",{name:"允许在后台完成回应时提示我"});expect(f.save).toHaveBeenCalledOnce();
});
it("does not call a quiet-suppressed notification sent",async()=>{
  const f=fixture();f.read.mockResolvedValue({...initial,preferences:{...initial.preferences,enabled:true}});f.test.mockResolvedValue({status:"quiet"});
  render(<NotificationSettings transport={f.transport} profileId="local"/>);
  fireEvent.click(await screen.findByRole("button",{name:"发送测试通知"}));
  expect(await screen.findByText("当前处于安静时段，测试通知没有发送。")).toBeInTheDocument();
  expect(screen.queryByText(/Windows 已接收测试通知请求/)).not.toBeInTheDocument();
});
it("shows the OS-disabled state and does not permit a misleading notification test",async()=>{
  const f=fixture();f.read.mockResolvedValue({...initial,os_permission:"disabled",preferences:{...initial.preferences,enabled:true}});
  render(<NotificationSettings transport={f.transport} profileId="local"/>);
  expect(await screen.findByText(/系统已禁用/)).toBeInTheDocument();expect(screen.getByRole("button",{name:"发送测试通知"})).toBeDisabled();
  expect(f.test).not.toHaveBeenCalled();
});
it("allows an explicit first test when Windows has not reported a setting, without claiming permission",async()=>{
  const f=fixture();f.read.mockResolvedValue({...initial,os_permission:"unavailable",preferences:{...initial.preferences,enabled:true}});
  render(<NotificationSettings transport={f.transport} profileId="local"/>);
  expect(await screen.findByText(/尚未确认。保存偏好后可发送测试通知/)).toBeInTheDocument();
  expect(screen.queryByText(/允许接收；/)).not.toBeInTheDocument();
  expect(f.test).not.toHaveBeenCalled();
  const testButton=screen.getByRole("button",{name:"发送测试通知"});
  expect(testButton).toBeEnabled();fireEvent.click(testButton);
  expect(await screen.findByText(/Windows 已接收测试通知请求/)).toBeInTheDocument();
  expect(f.test).toHaveBeenCalledOnce();
});
it("labels midnight and full-day quiet correctly, and rejects late settings from another profile",async()=>{
  const f=fixture();let complete!:(v:NotificationSettingsView)=>void;
  f.read.mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve;})).mockResolvedValue({...initial,preferences:{...initial.preferences,quiet_end:1320}});
  const view=render(<NotificationSettings transport={f.transport} profileId="local"/>);
  view.rerender(<NotificationSettings transport={f.transport} profileId="another"/>);
  expect(await screen.findByText("起止时间相同：全天保持安静。")).toBeInTheDocument();
  await act(async()=>complete(initial));expect(screen.getByLabelText("安静时段结束")).toHaveValue("22:00");
});
it("keeps unread notification state unknown instead of showing a default off switch",async()=>{
  const f=fixture();f.read.mockRejectedValue(new Error("test error"));render(<NotificationSettings transport={f.transport} profileId="local"/>);
  expect(await screen.findByRole("alert")).toHaveTextContent("当前状态未确认");expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
});
