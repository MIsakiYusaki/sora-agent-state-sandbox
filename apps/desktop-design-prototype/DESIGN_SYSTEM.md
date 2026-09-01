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
