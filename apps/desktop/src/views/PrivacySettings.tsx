import { useEffect, useRef, useState } from "react";
import type { DesktopTransport } from "../transport";
import type { PrivacyExportKind } from "../transport/DesktopTransport";
import { SpaceErasureSettings } from "./SpaceErasureSettings";

export function PrivacySettings({ transport, profileId }: { transport: DesktopTransport; profileId: string | null }) {
  const [busy, setBusy] = useState<PrivacyExportKind | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [erasureBlocked, setErasureBlocked] = useState(true);
  const pending = useRef(false);
  const sequence = useRef(0);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    pending.current = false;
    setBusy(null);
    setError(null);
    setNotice(null);
    return () => {
      mounted.current = false;
      sequence.current += 1;
      pending.current = false;
    };
  }, [transport, profileId]);

  const exportData = async (kind: PrivacyExportKind) => {
    if (!profileId || pending.current || erasureBlocked) return;
    pending.current = true;
    const requestSequence = ++sequence.current;
    const current = () => mounted.current && sequence.current === requestSequence;
    setBusy(kind);
    setError(null);
    setNotice(null);
    try {
      const receipt = await transport.exportPrivacy({ kind, expected_profile_id: profileId, confirmed: true });
      if (!current()) return;
      if (receipt.schema_version !== "DesktopPrivacyExportReceipt.v1" || receipt.kind !== kind) {
        throw new Error("Unconfirmed export receipt");
      }
      if (receipt.status === "cancelled") {
        setNotice("已取消保存。");
      } else if (receipt.status === "saved" && typeof receipt.bytes === "number" && Number.isInteger(receipt.bytes) && receipt.bytes > 0) {
        setNotice(kind === "memory_summary" ? "记忆摘要已保存到你选择的位置。" : "诊断文件已保存到你选择的位置。");
      } else {
        throw new Error("Unconfirmed file save");
      }
    } catch {
      if (current()) setError("没有获得文件保存成功的确认。请检查保存位置，再重新发起导出。");
    } finally {
      if (current()) {
        pending.current = false;
        setBusy(null);
      }
    }
  };

  return <section className="space-settings" aria-label="数据与隐私">
    <div className="settings-heading"><div><h2>数据与隐私</h2><p>为当前空间保存一份可查看的资料。</p></div></div>
    <p>点击导出后，在 Windows 保存窗口选择位置。文件格式为 JSON。</p>
    <div className="privacy-section">
      <h3>记忆摘要</h3>
      <p>导出当前允许显示的低敏记忆摘要。已隐藏、已停用和受保护的内容会被排除。</p>
      <button className="button" type="button" disabled={!!busy || !profileId || erasureBlocked} onClick={() => void exportData("memory_summary")}>导出记忆摘要</button>
    </div>
    <div className="privacy-section">
      <h3>诊断文件</h3>
      <p>包括协议与运行环境版本、记忆数量和状态计数，便于核对当前空间的基本情况。</p>
      <button className="button button--secondary" type="button" disabled={!!busy || !profileId || erasureBlocked} onClick={() => void exportData("diagnostic")}>导出诊断文件</button>
    </div>
    {busy && <p role="status">请选择保存位置，正在准备导出…</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    <div className="privacy-section"><h3>管理已保存的记忆</h3><p>可以在“记忆与隐私”页查看、隐藏、纠错或忘记单条内容。</p></div>
    <SpaceErasureSettings transport={transport} profileId={profileId} disabled={!!busy} onBlockedChange={setErasureBlocked} />
  </section>;
}
