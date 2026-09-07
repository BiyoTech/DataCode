# frontend

The single-page UI (Vue 3 + Vite + TypeScript, `:5173` in dev). It talks to
the gateway (`:8000`) and to NOTHING else — the vite dev proxy and the
production nginx both forward `/api` to the gateway, so no backend service is
ever directly reachable from the browser.

## Stack

| Concern | Choice |
|---|---|
| Framework | Vue 3.5 (composition API), vue-router 4 |
| State | Pinia (`src/stores/`) |
| UI kit | TDesign + Tailwind CSS 4 |
| HTTP | axios (`src/utils/` — interceptors live here) |
| i18n | vue-i18n (`src/i18n/`) — Chinese/English switch |
| Editors / viz | monaco-editor, echarts, @antv/g6, mermaid, markdown-it + KaTeX |

## Layout

```
src/
├── views/         route-level pages
├── components/    feature components
├── stores/        Pinia stores — one per backend domain (quotas, sessions, …)
├── composables/   shared reactive logic
├── i18n/          locale messages (zh/en)
├── router/        routes
├── utils/         axios instance + interceptors, relocating banner glue
└── types/         API payload types
```

## Platform contracts the UI implements

- **UTC everywhere on the wire.** The backend stores and compares UTC only;
  this app converts to the viewer's timezone for display (auto-detected,
  manually overridable — the user's timezone preference is carried on the
  identity profile).
- **i18n** — every user-facing string goes through the locale files; error
  copy for the frozen contract codes (quota 429s, state-schema mismatch, …)
  is keyed by `errors.*`.
- **Transparent relocation** — the axios interceptor retries
  `503 WORKSPACE_RELOCATING` with the advertised `Retry-After` while
  `WorkspaceRelocatingBanner` shows a global banner; the caller only sees an
  error after the retry budget is spent.
- **XSS** — all rendered markdown passes through DOMPurify; the KaTeX and
  mermaid paths use their safe modes.

## Run

```
npm install
npm run dev          # vite, proxying /api → localhost:8000
npm run build        # vue-tsc + vite build (Dockerfile runs this)
```
