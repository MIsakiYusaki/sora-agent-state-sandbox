import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { HistoryPage } from "./contracts/generated";
import { App } from "./App";
import { MockDesktopTransport } from "./transport/MockDesktopTransport";

afterEach(() => window.localStorage.clear());
const conversations = ["alpha", "beta"].map(id => ({ conversation_id: `conversation_${id}`, title: `历史${id}`, preview: "保存的摘要", created_at_ms: 1000, updated_at_ms: 2000, message_count: 1, conversation_version: 1 }));
const history = (conversationId: string, content: string): HistoryPage => ({ conversation_id: conversationId, conversation_version: 1, items: [{ role: "user", content, turn_id: "turn_history_fixture", sequence: 1, created_at_ms: 1000 }], next_before_sequence: null });
function fixture() {
  const transport = new MockDesktopTransport();
  const list = vi.spyOn(transport, "listConversations").mockResolvedValue({ schema_version: "ConversationIndex.v1", items: conversations });
  const load = vi.spyOn(transport, "loadHistory").mockImplementation(async request => history(request.conversation_id, request.conversation_id === "conversation_main" ? "当前对话正文" : "选中对话正文"));
  return { transport, list, load };
}

it("keeps the latest selected history preview when an earlier conversation arrives late", async () => {
  const f = fixture(); let complete!: (value: HistoryPage) => void;
  f.load.mockImplementation(request => request.conversation_id === "conversation_alpha" ? new Promise(resolve => { complete = resolve; }) : Promise.resolve(history(request.conversation_id, "beta 正文")));
  render(<App transport={f.transport} />); await screen.findByRole("textbox", { name: "给 Sora 发消息" });
  fireEvent.click(screen.getByRole("button", { name: "历史" }));
  await waitFor(() => expect(f.load).toHaveBeenCalledWith(expect.objectContaining({ conversation_id: "conversation_alpha" })));
  fireEvent.click(screen.getByRole("button", { name: /历史beta.*1 条消息/ }));
  expect(await screen.findByText("beta 正文")).toBeInTheDocument();
  await act(async () => complete(history("conversation_alpha", "不得串入的 alpha 正文")));
  expect(screen.queryByText("不得串入的 alpha 正文")).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "历史beta" })).toBeInTheDocument();
});

it("refuses a response with a different conversation identity", async () => {
  const f = fixture(); f.load.mockImplementation(async request => request.conversation_id === "conversation_main" ? history(request.conversation_id, "当前对话正文") : history("foreign_conversation", "不得披露的错误归属正文"));
  render(<App transport={f.transport} />); await screen.findByRole("textbox", { name: "给 Sora 发消息" });
  fireEvent.click(screen.getByRole("button", { name: "历史" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("这段对话读取失败");
  expect(screen.queryByText("不得披露的错误归属正文")).not.toBeInTheDocument();
  expect(screen.queryByText("这段对话没有可显示的消息。")).not.toBeInTheDocument();
});

it("does not let a delayed index refresh override a user's newer preview selection", async () => {
  const f = fixture(); render(<App transport={f.transport} />);
  await screen.findByText("当前对话正文"); await waitFor(() => expect(f.list).toHaveBeenCalledOnce());
  let complete!: (value: Awaited<ReturnType<typeof f.transport.listConversations>>) => void;
  f.list.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "历史" }));
  fireEvent.click(screen.getByRole("button", { name: /历史beta.*1 条消息/ }));
  await screen.findByRole("heading", { name: "历史beta" });
  await act(async () => complete({ schema_version: "ConversationIndex.v1", items: conversations }));
  expect(screen.getByRole("heading", { name: "历史beta" })).toBeInTheDocument();
  expect(f.load).not.toHaveBeenCalledWith(expect.objectContaining({ conversation_id: "conversation_alpha" }));
});


it("older-message failure cannot fail a newly submitted turn or erase its draft", async () => {
  const f=fixture(); let rejectEarlier!: (error:Error)=>void;
  f.load.mockImplementation(request=>request.before_sequence != null
    ? new Promise((_resolve,reject)=>{rejectEarlier=reject;})
    : Promise.resolve({...history(request.conversation_id,"已保存的最近消息"),items:[{...history(request.conversation_id,"已保存的最近消息").items[0],sequence:51}],next_before_sequence:51}));
  vi.spyOn(f.transport,"submitTurn").mockImplementation(async request=>({schema_version:"TurnAccepted.v1",turn_id:"qa-active-turn",conversation_id:request.conversation_id,correlation_id:"a".repeat(32),request_id:"qa-request",status:"accepted",conversation_version:1,events_url:"/qa-events",accepted_at_ms:1000,idempotent_replay:false}));
  render(<App transport={f.transport}/>); await screen.findByText("已保存的最近消息");
  fireEvent.click(screen.getByRole("button",{name:"加载更早消息"}));
  fireEvent.change(screen.getByRole("textbox",{name:"给 Sora 发消息"}),{target:{value:"新的一轮对话"}});
  fireEvent.click(screen.getByRole("button",{name:/^发送$/}));
  await waitFor(()=>expect(f.transport.submitTurn).toHaveBeenCalledOnce());
  await act(async()=>rejectEarlier(new Error("history read failed")));
  expect(await screen.findByRole("alert")).toHaveTextContent("不会影响正在发送的对话");
  expect(screen.queryByText("本轮未完成")).not.toBeInTheDocument();
  expect(screen.getByText("Sora 正在回应")).toBeInTheDocument();
});

it("does not reuse an older-message result after switching A to B and back to A", async () => {
  const f=fixture(); let old!: (page:HistoryPage)=>void;
  f.load.mockImplementation(request=>request.before_sequence != null
    ? new Promise(resolve=>{old=resolve;})
    : Promise.resolve({...history(request.conversation_id,"最近消息 "+request.conversation_id),items:[{...history(request.conversation_id,"最近消息 "+request.conversation_id).items[0],sequence:101}],next_before_sequence:101}));
  render(<App transport={f.transport}/>); await screen.findByText("最近消息 conversation_main");
  const continueWith=async(id:string)=>{
    fireEvent.click(screen.getByRole("button",{name:"历史"}));
    await screen.findByRole("button",{name:new RegExp("历史"+id+".*1 条消息")});
    fireEvent.click(screen.getByRole("button",{name:new RegExp("历史"+id+".*1 条消息")}));
    await screen.findByRole("heading",{name:"历史"+id});
    fireEvent.click(screen.getByRole("button",{name:"继续对话"}));
    await screen.findByText("最近消息 conversation_"+id);
  };
  await continueWith("alpha");
  fireEvent.click(screen.getByRole("button",{name:"加载更早消息"}));
  await continueWith("beta"); await continueWith("alpha");
  await act(async()=>old(history("conversation_alpha","不得采用的旧分页回包")));
  expect(screen.queryByText("不得采用的旧分页回包")).not.toBeInTheDocument();
  expect(screen.getByRole("button",{name:"加载更早消息"})).toBeEnabled();
});
