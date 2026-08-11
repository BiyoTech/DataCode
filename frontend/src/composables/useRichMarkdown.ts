/**
 * Rich Markdown rendering composable.
 *
 * Shared renderer used by both the full-page MarkdownPreview and the chat
 * MessageBubble so their rich-rendering behaviour stays identical (no
 * duplicated logic). It renders Markdown to sanitized HTML and hydrates
 * special fenced code blocks into live diagrams/charts:
 *
 *  - Standard CommonMark via markdown-it
 *  - LaTeX math (inline $...$ / block $$...$$) via KaTeX
 *  - Mermaid diagrams (```mermaid / ```flowchart) via mermaid.js (lazy-loaded)
 *  - ECharts charts (```echarts, body = JSON option) via echarts (lazy-loaded)
 *  - XSS protection via DOMPurify (svg tags/attrs allowed for rendered diagrams)
 *
 * Security: chart options are parsed with JSON.parse only (never eval), and all
 * HTML passes through DOMPurify before injection.
 */
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import katex from 'katex'

let katexCssLoaded = false

async function ensureKatexCss() {
  if (katexCssLoaded) return
  katexCssLoaded = true
  await import('katex/dist/katex.min.css')
}

// Shared parser instance — configured once and reused across all callers.
const md = new MarkdownIt({
  html: false, // block raw HTML for security
  linkify: true,
  breaks: true,
  typographer: true,
})

// ────── LaTeX: inline $...$  /  block $$...$$ ──────
const LATEX_BLOCK_RE = /\$\$([\s\S]*?)\$\$/g
const LATEX_INLINE_RE = /\$(.*?)\$/g

function renderLatex(html: string): string {
  if (html.includes('$')) {
    void ensureKatexCss()
  }
  // Block math first (to avoid conflict with inline)
  html = html.replace(LATEX_BLOCK_RE, (_match: string, tex: string) => {
    try {
      return katex.renderToString(tex.trim(), { displayMode: true, throwOnError: false })
    } catch {
      return `<pre class="katex-error">${_match}</pre>`
    }
  })
  // Inline math
  html = html.replace(LATEX_INLINE_RE, (_match: string, tex: string) => {
    try {
      return katex.renderToString(tex.trim(), { displayMode: false, throwOnError: false })
    } catch {
      return `<span class="katex-error">${_match}</span>`
    }
  })
  return html
}

// ────── Mermaid / Flowchart code-block rendering ──────
// Mermaid is lazy-loaded so it never bloats the initial bundle.
async function renderMermaidDiagrams(root: HTMLElement) {
  const blocks = root.querySelectorAll<HTMLElement>(
    'pre code.language-mermaid:not([data-rendered]), pre code.language-flowchart:not([data-rendered])',
  )
  if (blocks.length === 0) return

  try {
    const mermaid = (await import('mermaid')).default
    mermaid.initialize({ startOnLoad: false, theme: 'dark' })

    for (const block of blocks) {
      // Mark early so concurrent/streaming re-renders skip this block.
      block.setAttribute('data-rendered', 'true')
      const pre = block.parentElement!
      const id = `mermaid-${Math.random().toString(36).slice(2, 9)}`
      const container = document.createElement('div')
      container.className = 'md-diagram'
      container.id = id
      container.textContent = block.textContent || ''
      pre.replaceWith(container)

      try {
        const { svg } = await mermaid.render(`${id}-svg`, block.textContent || '')
        container.innerHTML = svg
      } catch (err) {
        container.innerHTML = `<pre class="mermaid-error">Mermaid render error: ${String(err)}</pre>`
      }
    }
  } catch {
    // mermaid not available — leave code blocks as-is
  }
}

// ────── ECharts code-block rendering ──────
async function renderECharts(root: HTMLElement) {
  const blocks = root.querySelectorAll<HTMLElement>('pre code.language-echarts:not([data-rendered])')
  if (blocks.length === 0) return

  try {
    const echarts = await import('echarts')

    for (const block of blocks) {
      // Mark early so concurrent/streaming re-renders skip this block.
      block.setAttribute('data-rendered', 'true')
      const pre = block.parentElement!
      const container = document.createElement('div')
      container.className = 'md-echarts'
      pre.replaceWith(container)

      try {
        // Parse only — never eval — the chart option for security.
        const option = JSON.parse(block.textContent || '{}')
        const chart = echarts.init(container, undefined, {
          // Fall back to a sane width when the container is not yet laid out.
          width: container.clientWidth || undefined,
          height: 360,
        })
        chart.setOption(option)
        // Keep the chart responsive to container width changes.
        const observer = new ResizeObserver(() => chart.resize())
        observer.observe(container)
      } catch {
        container.innerHTML = `<pre class="echarts-error">ECharts JSON parse error</pre>`
      }
    }
  } catch {
    // echarts not available
  }
}

export function useRichMarkdown() {
  /**
   * Render a Markdown string into sanitized HTML.
   * KaTeX math is expanded here; echarts/mermaid blocks stay as code fences and
   * are hydrated later by renderExtensions once the HTML is mounted.
   */
  function render(text: string | null | undefined): string {
    if (!text) return ''
    let html = md.render(text)
    html = renderLatex(html)
    return DOMPurify.sanitize(html, {
      ADD_TAGS: ['svg', 'path', 'circle', 'line', 'rect', 'polygon', 'polyline', 'g', 'defs', 'use', 'text', 'tspan'],
      ADD_ATTR: ['d', 'cx', 'cy', 'r', 'x', 'y', 'width', 'height', 'viewBox', 'fill', 'stroke', 'stroke-width', 'transform', 'class', 'target', 'rel'],
    })
  }

  /**
   * Post-process a mounted root element, hydrating ```echarts and
   * ```mermaid / ```flowchart code blocks into live charts/diagrams.
   * Already-rendered blocks are skipped via a data-rendered marker, which makes
   * this safe to call repeatedly (e.g. during streaming updates).
   */
  async function renderExtensions(root: HTMLElement | null): Promise<void> {
    if (!root) return
    await Promise.all([renderMermaidDiagrams(root), renderECharts(root)])
  }

  return { render, renderExtensions }
}
