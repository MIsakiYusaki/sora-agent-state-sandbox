import { useCallback, useEffect, useRef, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { PrivacyState } from "../transport/DesktopTransport";

export function SpaceErasureSettings({ transport, profileId, disabled, onBlockedChange }: {
  transport: DesktopTransport;
  profileId: string | null;
  disabled: boolean;
  onBlockedChange: (blocked: boolean) => void;
}) {
  const [state, setState] = useState<PrivacyState | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const sequence = useRef(0);
  const mounted = useRef(false);
  const pending = useRef(false);

  const read = useCallback(async () => {
    if (!profileId || pending.current) return;
    pending.current = true;
    const token = ++sequence.current;
    const current = () => mounted.current && token === sequence.current;
    setBusy(true);
    setState(null);
    setError(null);
    setNotice(null);
    setConfirmation("");
    setChecked(false);
    onBlockedChange(true);
    try {
      const next = await transport.getPrivacyState();
      if (!current()) return;
      if (next.schema_version !== "DesktopPrivacyState.v1" || next.profile_id !== profileId
          || !/^[a-f0-9]{64}$/.test(next.revision) || !next.space_name
          || (next.pending_operation !== null && !/^erase_[a-f0-9]{32}$/.test(next.pending_operation))
          || (next.last_completed_operation !== null && !/^erase_[a-f0-9]{32}$/.test(next.last_completed_operation))) {
        throw new Error("Invalid privacy state");
      }
      setState(next);
      onBlockedChange(next.pending_operation !== null);
    } catch {
      if (current()) setError("空间删除状态读取失败。请重新读取后核对。");
    } finally {
      if (current()) { pending.current = false; setBusy(false); }
    }
  }, [transport, profileId, onBlockedChange]);

  useEffect(() => {
    mounted.current = true;
    pending.current = false;
    setState(null);
    setBusy(false);
    onBlockedChange(true);
    void read();
    return () => { mounted.current = false; sequence.current += 1; pending.current = false; };
  }, [read, onBlockedChange]);

  const erase = async () => {
    if (!state || !profileId || disabled || pending.current || confirmation !== "删除当前空间" || !checked) return;
    pending.current = true;
    const token = ++sequence.current;
    const current = () => mounted.current && token === sequence.current;
    setBusy(true);
    setError(null);
    setNotice(null);
    setChecked(false);
    setConfirmation("");
    onBlockedChange(true);
    try {
      await transport.eraseSpace({ expected_profile_id: profileId, expected_revision: state.revision, confirmation, confirmed: true });
      if (current()) setNotice("正在重新启动客户端。重新打开后会核对删除回执并进入空白空间。");
    } catch {
      if (current()) {
        setState(null);
        setError("删除未获得完整确认。请重新读取删除状态；若已开始删除，继续恢复会沿用同一次操作。");
      }
    } finally {
      if (current()) { pending.current = false; setBusy(false); }
    }
  };

  return <section className="privacy-section" aria-label="删除当前空间">
    <h3>删除当前空间</h3>
    <p>删除当前空间的对话、记忆、领域状态、模型配置、供应商密钥和通知偏好。完成后重启进入新的空白空间。</p>
    <p>你自行保存的导出文件和备份需要另外管理。</p>
    <button className="button button--secondary" type="button" disabled={busy || disabled || !profileId} onClick={() => void read()}>重新读取删除状态</button>
    {busy && <p role="status">正在核对空间删除操作，请等待结果…</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {state?.last_completed_operation && <p role="status">上次空间删除已完成。</p>}
    {state?.pending_operation && <p role="status">上次删除尚未完成。此空间已暂停打开，请确认后继续恢复删除。</p>}
    {state && <fieldset disabled={busy || disabled}>
      <legend>{state.pending_operation ? "恢复已开始的删除" : `确认删除“${state.space_name}”`}</legend>
      <label>请输入“删除当前空间”<input aria-label="删除当前空间确认文字" value={confirmation} autoComplete="off" onChange={event => setConfirmation(event.target.value)} /></label>
      <label className="space-confirm"><input type="checkbox" checked={checked} onChange={event => setChecked(event.target.checked)} />我理解当前空间的数据会被永久删除，并同意重新启动客户端</label>
      <button className="button button--danger" type="button" disabled={!checked || confirmation !== "删除当前空间"} onClick={() => void erase()}>{state.pending_operation ? "继续恢复删除" : "确认删除并重启"}</button>
    </fieldset>}
  </section>;
}
