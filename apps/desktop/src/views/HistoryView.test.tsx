import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { DesktopTransport, ConversationIndexView } from "../transport";
import { HistoryView } from "./HistoryView";

afterEach(cleanup);
const row = (id: string) => ({ conversation_id:id,title:id,preview:"真实摘要",created_at_ms:1000,updated_at_ms:2000,message_count:100,conversation_version:50 });
function setup(listConversations: unknown) {
  const transport={listConversations} as unknown as DesktopTransport, select=vi.fn();
  render(<HistoryView transport={transport} conversations={[]} selectedId={null} preview={[]} loading={false} onSelect={select} onContinue={vi.fn()} onNew={vi.fn()} />);
  return {transport,select};
}

it("首批没有匹配时保留真实续查入口，可以找到第101段以前的正文", async () => {
  const list=vi.fn().mockResolvedValueOnce({schema_version:"ConversationIndex.v1",items:[],next_offset:100})
    .mockResolvedValueOnce({schema_version:"ConversationIndex.v1",items:[row("older-body-result")],next_offset:null});
  const {select}=setup(list);
  fireEvent.change(screen.getByRole("textbox",{name:"搜索对话"}),{target:{value:"正文关键词"}});
  await screen.findByRole("button",{name:"继续搜索更早对话"});
  expect(screen.queryByText("没有找到相关对话。")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"继续搜索更早对话"}));
  await userEvent.click(await screen.findByRole("button",{name:/older-body-result/}));
  expect(select).toHaveBeenCalledWith("older-body-result");
  expect(list).toHaveBeenLastCalledWith({query:"正文关键词",offset:100});
});

it("快速换关键词后拒绝旧结果和旧的续查游标", async () => {
  let old!: (page:ConversationIndexView)=>void;
  const list=vi.fn().mockImplementationOnce(()=>new Promise(resolve=>{old=resolve;}))
    .mockResolvedValue({schema_version:"ConversationIndex.v1",items:[row("new-result")],next_offset:null});
  setup(list);
  const input=screen.getByRole("textbox",{name:"搜索对话"});
  fireEvent.change(input,{target:{value:"旧关键词"}});
  await waitFor(()=>expect(list).toHaveBeenCalledTimes(1));
  fireEvent.change(input,{target:{value:"新关键词"}});
  await screen.findByRole("button",{name:/new-result/});
  await act(async()=>old({schema_version:"ConversationIndex.v1",items:[row("stale-result")],next_offset:100}));
  expect(screen.queryByRole("button",{name:/stale-result/})).not.toBeInTheDocument();
  expect(screen.queryByRole("button",{name:"继续搜索更早对话"})).not.toBeInTheDocument();
});

it("正文搜索失败有重试入口，失败不能冒充没有历史", async () => {
  const list=vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({schema_version:"ConversationIndex.v1",items:[row("retried")],next_offset:null});
  setup(list); fireEvent.change(screen.getByRole("textbox",{name:"搜索对话"}),{target:{value:"关键词"}});
  expect(await screen.findByRole("alert")).toHaveTextContent("不能据此认定没有相关对话");
  expect(screen.queryByText("没有找到相关对话。")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"重试正文搜索"}));
  expect(await screen.findByRole("button",{name:/retried/})).toBeInTheDocument();
});

it("连续点击续查只读取一页，游标不前进时保留已有结果并报错", async () => {
  let finish!: (page:ConversationIndexView)=>void;
  const list=vi.fn().mockResolvedValueOnce({schema_version:"ConversationIndex.v1",items:[row("first")],next_offset:100})
    .mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  setup(list); fireEvent.change(screen.getByRole("textbox",{name:"搜索对话"}),{target:{value:"关键词"}});
  const button=await screen.findByRole("button",{name:"继续搜索更早对话"});
  await userEvent.dblClick(button);
  expect(list).toHaveBeenCalledTimes(2);
  await act(async()=>finish({schema_version:"ConversationIndex.v1",items:[row("wrong-page")],next_offset:100}));
  expect(await screen.findByRole("alert")).toHaveTextContent("并非完整搜索结果");
  expect(screen.getByRole("button",{name:/first/})).toBeInTheDocument();
  expect(screen.queryByRole("button",{name:/wrong-page/})).not.toBeInTheDocument();
});
