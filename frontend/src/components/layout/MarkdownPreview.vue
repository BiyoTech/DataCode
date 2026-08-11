<script setup lang="ts">
/**
 * MarkdownPreview — full-page renderer for Markdown content with rich extensions.
 *
 * All rendering logic (markdown-it + KaTeX + Mermaid + ECharts + DOMPurify) lives
 * in the shared `useRichMarkdown` composable so this component and the chat
 * MessageBubble render identically. Here we only wire the composable to the DOM
 * and provide the full-page layout styling.
 */
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { useRichMarkdown } from '@/composables/useRichMarkdown'

const props = defineProps<{
  content: string
}>()

const previewEl = ref<HTMLElement | null>(null)
const { render, renderExtensions } = useRichMarkdown()

// Sanitized HTML (echarts/mermaid fences hydrated after mount).
const sanitizedHtml = computed(() => render(props.content))

// Re-render extensions when content changes.
watch(sanitizedHtml, async () => {
  await nextTick()
  await renderExtensions(previewEl.value)
})

// Initial render on mount.
onMounted(async () => {
  await nextTick()
  await renderExtensions(previewEl.value)
})
</script>

<template>
  <div ref="previewEl" class="md-preview" v-html="sanitizedHtml" />
</template>

<style>
/* ────── Preview base (unscoped so KaTeX/Mermaid styles cascade) ────── */
.md-preview {
  font-size: 14px;
  line-height: 1.7;
  color: var(--text-primary);
  padding: 24px 32px;
  overflow-y: auto;
  height: 100%;
  word-wrap: break-word;
}

.md-preview h1, .md-preview h2, .md-preview h3,
.md-preview h4, .md-preview h5, .md-preview h6 {
  margin-top: 1.4em;
  margin-bottom: 0.6em;
  font-weight: 600;
  color: var(--text-primary);
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.3em;
}

.md-preview h1 { font-size: 1.6em; }
.md-preview h2 { font-size: 1.35em; }
.md-preview h3 { font-size: 1.15em; }

.md-preview p { margin: 0.6em 0; }

.md-preview a {
  color: var(--accent);
  text-decoration: none;
}

.md-preview a:hover { text-decoration: underline; }

.md-preview code {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.88em;
  background: var(--bg-hover);
  padding: 1px 5px;
  border-radius: 4px;
  color: var(--accent);
}

.md-preview pre {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px 16px;
  overflow-x: auto;
}

.md-preview pre code {
  background: transparent;
  padding: 0;
  color: var(--text-primary);
}

.md-preview blockquote {
  border-left: 3px solid var(--accent);
  padding-left: 16px;
  margin: 0.8em 0;
  color: var(--text-secondary);
}

.md-preview ul, .md-preview ol {
  padding-left: 1.5em;
  margin: 0.6em 0;
}

.md-preview li { margin: 0.2em 0; }

.md-preview table {
  border-collapse: collapse;
  width: 100%;
  margin: 0.8em 0;
}

.md-preview th, .md-preview td {
  border: 1px solid var(--border);
  padding: 8px 12px;
  text-align: left;
}

.md-preview th {
  background: var(--bg-hover);
  font-weight: 600;
}

.md-preview img {
  max-width: 100%;
  border-radius: 6px;
}

.md-preview hr {
  border: none;
  border-top: 1px solid var(--border);
  margin: 1.2em 0;
}

/* ────── KaTeX ────── */
.md-preview .katex { font-size: 1.05em !important; }
.md-preview .katex-display { margin: 0.8em 0; }
.katex-error { color: var(--error); }

/* ────── Mermaid / diagrams ────── */
.md-preview .md-diagram {
  display: flex;
  justify-content: center;
  margin: 1em 0;
  overflow-x: auto;
}
.md-preview .md-diagram svg { max-width: 100%; }

/* ────── ECharts ────── */
.md-preview .md-echarts {
  width: 100%;
  min-height: 400px;
  margin: 1em 0;
}

/* ────── Error fallback ────── */
.mermaid-error { color: var(--error); font-size: 0.88em; }
.echarts-error { color: var(--error); }
</style>
