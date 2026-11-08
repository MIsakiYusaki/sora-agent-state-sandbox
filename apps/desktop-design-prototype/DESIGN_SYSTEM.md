# Project Sora Desktop Design System

## Visual Direction

Project Sora uses one visual direction: **Quiet Companion Desk**.

The desktop app should feel like an ongoing private conversation, not a model dashboard. Sora's presence comes from the cropped character portrait, the four-point star, restrained jade feedback, and the rhythm of the copy. The character artwork never becomes a permanent poster column.

Design dials:

- Variance: 4/10. Recognizable but operational.
- Motion: 3/10. Motion only explains state changes.
- Density: 6/10. Calm reading space with compact controls.

## Color Tokens

- Ink: #1E2727
- Porcelain: #FAFCFB
- Mist: #EDF1F0
- Jade: #1F7B70
- Cold blue: #3679C8
- Quiet violet: #7B66B8
- Warning coral: #B84D52

Jade is the only primary interaction accent. Blue and violet identify secondary system categories, never competing calls to action. Gradients, decorative blobs, and generic AI purple surfaces are prohibited.

## Typography

- Product text: Segoe UI Variable Text, Microsoft YaHei UI, PingFang SC
- Display headings: Segoe UI Variable Display
- Technical identifiers: Cascadia Code or Consolas

Normal product copy is Chinese and names what the user controls. Internal field names may appear only inside Developer Mode as secondary technical identifiers.

## Information Architecture

The permanent rail contains only:

1. Conversation
2. History
3. Memory and Privacy
4. Settings

Conversation preferences open from the current conversation and are not a top-level system. Developer Mode is disabled by default and can only be enabled from Advanced Settings.

## User And Engineering Boundary

The normal interface may explain:

- how Sora will respond in this conversation;
- which previously stated facts were used;
- whether memory was used naturally or mentioned explicitly;
- what the user can hide, forget, export, or disable.

The normal interface must not expose:

- response obligation, expansion permission, or policy matrices;
- model scores, hidden state dimensions, or relationship optimization metrics;
- pipeline stages, trace IDs, schema versions, or queue state.

Those belong to the Chinese Developer Mode. Code identifiers remain visible there only when needed for diagnosis.

## Layout

- Navigation rail: 56 px
- Conversation drawer: 268 px, optional
- Conversation reading column: 720 to 760 px
- Context drawer: 310 px, optional
- Page content: 820 px for focused preferences, 1040 to 1180 px for operational lists
- Corner radius: 4, 6, 7, and 10 px only

Cards are reserved for repeated items, modals, and framed tools. Page sections use spacing and dividers.

## Motion

- Hover and focus feedback: 120 to 160 ms
- Drawers and panels: 180 ms
- Streaming and reconnect feedback may repeat while active
- No decorative entrance choreography
- prefers-reduced-motion renders final states immediately

## Responsive And Accessibility

- Verify at 1440 x 900, 1024 x 768, and 420 x 560
- Preserve visible keyboard focus and semantic labels
- Do not rely on color alone for status
- Keep touch targets usable in compact mode
- Prevent horizontal page scrolling and text clipping
- Normal text contrast must meet WCAG AA

## Production Evolution Rules

The prototype is an executable product specification, not a frozen screenshot. It may be
refined when current runtime capabilities, privacy boundaries, or production usability expose
an incorrect interaction. Refinements must preserve Quiet Companion Desk, the four permanent
navigation entries, and the normal-user versus Developer Mode boundary.

Production pages use three evidence states:

- **Available now**: the production client is connected to a real typed Host or Gateway contract.
- **System active, detail bridge pending**: the canonical subsystem is instantiated and healthy,
  but per-turn Snapshot or Event detail is not yet projected to the desktop.
- **Unavailable**: the canonical capability itself is absent, disabled, or unhealthy.

Do not collapse the second and third states into one label such as “未接入”. Do not use static
success data to make a pending bridge appear complete.

## Conversation Surface

- User messages use an ink-on-mist surface. They never use white text on a pale surface.
- Sora messages read as part of the conversation canvas, aligned with the avatar. They do not
  need a card border unless a message has a distinct warning or recovery state.
- The composer contains the current response style and follow-up summary on its lower edge.
  The summary opens “这次想怎么聊” and reflects the real conversation control contract.
- Streaming, pending, failed, stopped, offline, Provider-missing, and repair-required remain
  visually distinct without moving the composer.

## History Surface

History is a reading and return workspace, not an analytics page.

- Use a compact functional header, search, a conversation list, and a readable preview.
- Do not show conversation versions, schema identifiers, message-type tags, decorative metrics,
  or disabled filters in the normal interface.
- Branch controls appear only when a real branch contract exists. Until then, the page restores
  canonical conversations without explaining missing APIs to the user.

## Memory And Privacy Surface

Memory and Privacy is a data-rights workspace, not a memory scoring dashboard.

- Rows may show a user-facing memory kind and whether the memory is currently hidden.
- Detail explains the saved gist, the user-visible evidence basis, whether it may affect replies,
  and the available hide, restore, correct, export, or forget actions.
- Confidence percentages, sensitivity bands, lifecycle tiers, Gate state, internal evidence IDs,
  and retrieval scores never appear in the normal interface.
- Counts are supporting context only. Do not lead with a four-metric dashboard.

## Developer Mode Surface

Developer Mode is a runtime inspector attached to the same profile, conversation, and event
stream as the production conversation.

- Its landing state first answers which systems are running, which evidence is already visible,
  and which desktop bridges are still pending.
- Turn Timeline can use real Gateway events already delivered to the client. It must not invent a
  latency waterfall when detailed spans are unavailable.
- Provider Routes, Memory health, async backlog, subsystem health, and Module 10 evidence come
  from a capability-gated, redacted snapshot.
- Per-turn typed Frames and controlled commands remain explicitly marked as bridge work until
  their Snapshot/Event/Command contracts are available.
- API keys, capability tokens, raw prompts, chain-of-thought, filesystem paths, and unrestricted
  Shell, SQL, Python, or eval access remain forbidden in every state.
