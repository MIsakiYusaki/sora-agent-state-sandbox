import { useLayoutEffect, useRef, type UIEvent } from "react";

type Anchor = { key: string | null; offset: number; height: number; top: number };

/** Preserve an actually visible message while prepending; only follow new text
 * when the reader was already at the bottom. No history content is persisted. */
export function useHistoryScroll({ scope, firstKey, revision, loadingEarlier }: {
  scope: string; firstKey?: string; revision: string; loadingEarlier: boolean;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const restore = useRef<Anchor | null>(null);
  const previous = useRef<{ scope: string; revision: string } | null>(null);

  function capture(viewport: HTMLDivElement): Anchor {
    const top = viewport.getBoundingClientRect().top;
    const row = [...viewport.querySelectorAll<HTMLElement>("[data-history-key]")]
      .find(item => item.getBoundingClientRect().bottom > top + 1);
    return { key: row?.dataset.historyKey ?? null, offset: row ? row.getBoundingClientRect().top - top : 0,
      height: viewport.scrollHeight, top: viewport.scrollTop };
  }
  function rememberPrepend() {
    if (viewportRef.current) restore.current = capture(viewportRef.current);
  }
  function onScroll(event: UIEvent<HTMLDivElement>) {
    const viewport = event.currentTarget;
    nearBottom.current = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 72;
    // If the user keeps reading during the request, preserve that newer place.
    if (restore.current) restore.current = capture(viewport);
  }
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const before = previous.current;
    if (!before || before.scope !== scope) {
      restore.current = null;
      nearBottom.current = true;
      viewport.scrollTop = viewport.scrollHeight;
    } else if (restore.current && !loadingEarlier) {
      const saved = restore.current;
      restore.current = null;
      const anchor = [...viewport.querySelectorAll<HTMLElement>("[data-history-key]")]
        .find(item => item.dataset.historyKey === saved.key);
      viewport.scrollTop = anchor
        ? viewport.scrollTop + anchor.getBoundingClientRect().top - viewport.getBoundingClientRect().top - saved.offset
        : saved.top + viewport.scrollHeight - saved.height;
      nearBottom.current = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 72;
    } else if (!restore.current && nearBottom.current && before.revision !== revision) {
      viewport.scrollTop = viewport.scrollHeight;
    }
    previous.current = { scope, revision };
  }, [scope, firstKey, revision, loadingEarlier]);
  return { viewportRef, rememberPrepend, onScroll };
}
