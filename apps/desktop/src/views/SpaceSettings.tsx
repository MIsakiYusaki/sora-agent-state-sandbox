import { useEffect, useRef, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { SpacePage } from "../transport/DesktopTransport";

export function SpaceSettings({ transport, profileId, switchingAllowed = true }: { transport: DesktopTransport; profileId: string | null; switchingAllowed?: boolean }) {
  const [page, setPage] = useState<SpacePage | null>(null);
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const epoch = useRef(0), mounted = useRef(false), pending = useRef(false);
  const begin = () => { if (pending.current) return null; pending.current = true; setBusy(true); setError(null); setNotice(null); setConfirmed(false); return ++epoch.current; };
  const current = (token: number) => mounted.current && token === epoch.current;
  const finish = (token: number) => { if (current(token)) { pending.current = false; setBusy(false); } };
  const validPage = (value: SpacePage) => value.active_id === profileId && value.spaces.some(item => item.id === profileId);
  const read = async () => {
    const token = begin(); if (token === null) return;
    setPage(null); setTarget("");
    try {
      const next = await transport.listSpaces(); if (!current(token)) return;
      if (!validPage(next)) throw new Error("space owner mismatch");
      setPage(next);
    } catch { if (current(token)) setError("空间目录读取失败或与当前身份不一致，请重新读取。不会显示为空空间或切换身份。"); }
    finally { finish(token); }
  };
  useEffect(() => {
    mounted.current = true; pending.current = false; setPage(null); setTarget(""); setConfirmed(false); setError(null); setNotice(null); setBusy(false);
    if (profileId) void read();
    return () => { mounted.current = false; ++epoch.current; pending.current = false; };
  }, [transport, profileId]);
  const create = async () => {
    if (!page || !name.trim()) return;
    const token = begin(); if (token === null) return;
    try {
      const next = await transport.createSpace(name.trim(), page.revision); if (!current(token)) return;
      if (!validPage(next)) throw new Error("space owner mismatch");
      setPage(next); setName(""); setNotice("独立空间已创建。当前仍在原空间；选择新空间并确认切换后才会重启。");
    } catch { if (current(token)) { setPage(null); setTarget(""); setError("创建结果未获完整确认，请重新读取空间目录后核对，不会自动重复创建。"); } }
    finally { finish(token); }
  };
  const switchSpace = async () => {
    if (!page || !target || target === page.active_id || !confirmed || !switchingAllowed) return;
    const token = begin(); if (token === null) return;
    try {
      await transport.switchSpace({ space_id: target, expected_revision: page.revision, confirmed: true });
      if (current(token)) setNotice("正在重新启动客户端以载入新空间…");
    } catch { if (current(token)) { setPage(null); setTarget(""); setError("切换未获得完整确认。请重新读取目录并核对当前空间；不会自动重复切换。"); } }
    finally { finish(token); }
  };
  const openLocation = async () => {
    const token = begin(); if (token === null) return;
    try { await transport.openSpaceLocation(); if (current(token)) setNotice("已请求 Windows 打开当前空间的数据位置。"); }
    catch { if (current(token)) setError("数据位置未能打开，请检查客户端连接后重试。"); }
    finally { finish(token); }
  };
  return <section className="space-settings" aria-label="独立数据空间">
    <div className="settings-heading"><div><h2>Sora 与我</h2><p>每个独立空间分别保存对话、记忆、模型配置和治理记录。</p></div></div>
    <p>当前数据空间：{profileId ?? "尚未确认"}{page ? ` · ${page.spaces.find(item => item.id === profileId)?.name}` : ""}</p>
    <div className="space-settings-tools"><button className="button button--secondary" type="button" disabled={busy || !profileId} onClick={() => void read()}>重新读取空间目录</button><button className="button button--secondary" type="button" disabled={busy || !profileId} onClick={() => void openLocation()}>打开数据位置</button></div>
    {busy && <p role="status">正在核对空间操作…</p>}{error && <p className="inline-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {page && <><fieldset disabled={busy}><legend>新建独立空间</legend><p>创建空白空间，不复制当前对话、记忆或供应商密钥。</p><label>空间名称<input aria-label="新空间名称" maxLength={60} value={name} onChange={event => setName(event.target.value)} /></label><button className="button" type="button" disabled={!name.trim()} onClick={() => void create()}>创建独立空间</button></fieldset>
      <fieldset disabled={busy || !switchingAllowed}><legend>切换独立空间</legend><label>目标空间<select aria-label="目标独立空间" value={target} onChange={event => { setTarget(event.target.value); setConfirmed(false); }}><option value="">选择其他空间</option>{page.spaces.filter(item => item.id !== page.active_id).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><p>切换会安全停止当前空间并重新启动客户端。当前未发送的草稿会清空，已经保存的对话和记忆会保留。</p><label className="space-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认切换到所选空间并重新启动客户端</label><button className="button" type="button" disabled={!target || !confirmed} onClick={() => void switchSpace()}>确认切换并重启</button></fieldset>
      {!switchingAllowed && <p role="status">当前对话尚未结束，请等待本轮完成或明确停止后再切换。</p>}
    </>}
  </section>;
}
