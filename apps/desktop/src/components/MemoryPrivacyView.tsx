import { ArrowLeft, FileDown, LockKeyhole, ShieldCheck, Trash2 } from "lucide-react";

export function MemoryPrivacyView({ onBack }: { readonly onBack: () => void }) {
  return (
    <section className="memory-page page" aria-labelledby="memory-title">
      <header className="page-header">
        <button className="back-button" type="button" onClick={onBack}>
          <ArrowLeft size={17} aria-hidden="true" />
          返回对话
        </button>
        <div>
          <h1 id="memory-title">记忆与隐私</h1>
          <p>这里只展示你能理解和控制的内容，不会出现内部评分、推理过程或系统术语。</p>
        </div>
        <span className="page-status page-status--pending">功能准备中</span>
      </header>

      <div className="privacy-intro">
        <ShieldCheck size={22} aria-hidden="true" />
        <div>
          <h2>记忆操作暂未开放</h2>
          <p>当前桌面端还没有连接可靠的记忆读取与写入接口，因此不会显示示例记忆，也不会提供看似可用但实际无效的操作。</p>
        </div>
      </div>

      <section className="privacy-principles" aria-labelledby="privacy-principles-title">
        <h2 id="privacy-principles-title">接入后你可以做什么</h2>
        <div className="privacy-principles__grid">
          <article>
            <LockKeyhole size={19} aria-hidden="true" />
            <h3>查看保存内容</h3>
            <p>用自然语言说明保存了什么、来自哪里，以及是否会影响回应。</p>
          </article>
          <article>
            <FileDown size={19} aria-hidden="true" />
            <h3>导出自己的数据</h3>
            <p>执行前先预览范围，并明确区分对话、记忆和诊断信息。</p>
          </article>
          <article>
            <Trash2 size={19} aria-hidden="true" />
            <h3>撤销和删除</h3>
            <p>每项长期保存都需要提供可撤销入口，删除前会再次确认具体范围。</p>
          </article>
        </div>
      </section>

      <div className="privacy-footnote">
        当前页面只说明产品边界，不读取或修改任何记忆数据。
      </div>
    </section>
  );
}
