<script setup lang="ts">
/**
 * EditorPanel - Code editor with tab management, file save support, and
 * Markdown preview with rich rendering (LaTeX, Mermaid, ECharts, flowchart).
 *
 * For `.md` files, a mode switcher appears in the tab-bar right area:
 *  - Code     (Monaco editor only)
 *  - Split    (Monaco left + MarkdownPreview right, resizeable)
 *  - Preview  (MarkdownPreview only)
 *
 * Uses Monaco Editor for syntax highlighting and editing.
 * Falls back to simple textarea if Monaco fails to load.
 */
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFileStore } from '@/stores/files'
import { useWorkspaceStore } from '@/stores/workspace'
import { useHorizontalResize } from '@/composables/useResize'
import MarkdownPreview from './MarkdownPreview.vue'
import SplitHandle from '@/components/common/SplitHandle.vue'
import IconButton from '@/components/common/IconButton.vue'

const { t } = useI18n()
const fileStore = useFileStore()
const workspaceStore = useWorkspaceStore()

const editorContainer = ref<HTMLDivElement | null>(null)
let monacoEditor: any = null
let monaco: any = null
// Models backing the diff editor (disposed alongside the editor).
let diffModels: { original: any; modified: any } | null = null
// Whether the current Monaco instance is a diff editor (vs a plain editor).
let isDiffEditor = false
const monacoLoaded = ref(false)
const fallbackMode = ref(false)

// ────── Markdown preview mode support ──────
type MdViewMode = 'code' | 'split' | 'preview'
const mdViewMode = ref<MdViewMode>('code')

const isMarkdownFile = computed(() => {
  const f = fileStore.activeFile
  if (!f) return false
  return f.language === 'markdown' || f.name.endsWith('.md')
})

// Split-pane resize for markdown (300px initial code pane).
const mdSplitResize = useHorizontalResize(300, 120, () => {
  const el = document.querySelector('.editor-split-area')
  return (el?.clientWidth || 600) - 40
})
const codePaneWidth = computed(() => {
  if (mdViewMode.value !== 'split') return '100%'
  return mdSplitResize.size.value + 'px'
})

function onMdSplitResize(delta: number) {
  mdSplitResize.resizeByDelta(delta)
}

// When switching away from a .md file, reset mode to code.
watch(isMarkdownFile, (val) => {
  if (!val) mdViewMode.value = 'code'
})

// Load Monaco Editor dynamically with worker setup
onMounted(async () => {
  try {
    // Configure Monaco workers before importing
    self.MonacoEnvironment = {
      getWorker(_: string, label: string) {
        if (label === 'json') {
          return new Worker(
            new URL('monaco-editor/esm/vs/language/json/json.worker.js', import.meta.url),
            { type: 'module' }
          )
        }
        if (label === 'typescript' || label === 'javascript') {
          return new Worker(
            new URL('monaco-editor/esm/vs/language/typescript/ts.worker.js', import.meta.url),
            { type: 'module' }
          )
        }
        return new Worker(
          new URL('monaco-editor/esm/vs/editor/editor.worker.js', import.meta.url),
          { type: 'module' }
        )
      },
    }

    monaco = await import('monaco-editor')
    monacoLoaded.value = true
  } catch {
    // Fallback to simple textarea mode
    fallbackMode.value = true
  }
})

onBeforeUnmount(() => {
  disposeEditor()
})

function disposeEditor() {
  if (monacoEditor) {
    monacoEditor.dispose()
    monacoEditor = null
  }
  if (diffModels) {
    diffModels.original.dispose()
    diffModels.modified.dispose()
    diffModels = null
  }
  isDiffEditor = false
}

function initEditor() {
  if (!editorContainer.value || !monaco) return

  const file = fileStore.activeFile
  if (!file) return

  // Dispose previous instance if exists
  disposeEditor()

  // Read-only side-by-side diff (before vs after).
  if (file.isDiff) {
    monacoEditor = monaco.editor.createDiffEditor(editorContainer.value, {
      theme: 'vs-dark',
      readOnly: true,
      renderSideBySide: true,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, monospace",
      minimap: { enabled: false },
      automaticLayout: true,
      scrollBeyondLastLine: false,
    })
    const original = monaco.editor.createModel(file.diffOriginal || '', file.language)
    const modified = monaco.editor.createModel(file.content || '', file.language)
    monacoEditor.setModel({ original, modified })
    diffModels = { original, modified }
    isDiffEditor = true
    return
  }

  monacoEditor = monaco.editor.create(editorContainer.value, {
    value: file.content || '',
    language: file.language || 'sql',
    theme: 'vs-dark',
    readOnly: file.readOnly || false,
    fontSize: 13,
    fontFamily: "'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, monospace",
    minimap: { enabled: true },
    automaticLayout: true,
    scrollBeyondLastLine: false,
    tabSize: 2,
    wordWrap: 'off',
    lineNumbers: 'on',
    renderWhitespace: 'selection',
    bracketPairColorization: { enabled: true },
  })
  isDiffEditor = false

  // Listen for content changes
  monacoEditor.onDidChangeModelContent(() => {
    if (fileStore.activeFilePath) {
      const content = monacoEditor.getValue()
      fileStore.updateFileContent(fileStore.activeFilePath, content)
    }
  })

  // Ctrl+S / Cmd+S to save
  monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
    handleSave()
  })
}

// Watch editorContainer ref — when the DOM element appears, create the editor
watch(editorContainer, (el) => {
  if (el && monaco && fileStore.activeFile) {
    nextTick(() => initEditor())
  }
})

// Update editor when active file changes
watch(
  () => fileStore.activeFile,
  (file) => {
    if (!file) {
      disposeEditor()
      return
    }
    // Reuse the plain editor in place only when neither old nor new tab is a diff.
    if (monacoEditor && !isDiffEditor && !file.isDiff) {
      const currentValue = monacoEditor.getValue()
      if (currentValue !== file.content) {
        monacoEditor.setValue(file.content)
      }
      const model = monacoEditor.getModel()
      if (model && monaco) {
        monaco.editor.setModelLanguage(model, file.language)
      }
      monacoEditor.updateOptions({ readOnly: file.readOnly || false })
    } else if (editorContainer.value && monaco) {
      // First mount, or the editor type changed (plain <-> diff): recreate it.
      nextTick(() => initEditor())
    }
  }
)

function selectTab(path: string) {
  fileStore.activeFilePath = path
}

function closeTab(path: string) {
  fileStore.closeFile(path)
}

async function handleSave() {
  const ws = workspaceStore.currentWorkspace
  if (!ws || !fileStore.activeFilePath || fileStore.activeFile?.readOnly) return
  await fileStore.saveFile(ws.path, fileStore.activeFilePath)
}

// Handle fallback textarea changes
function handleTextareaInput(event: Event) {
  const target = event.target as HTMLTextAreaElement
  if (fileStore.activeFilePath) {
    fileStore.updateFileContent(fileStore.activeFilePath, target.value)
  }
}

// Keyboard shortcut for save in fallback mode
function handleKeydown(event: KeyboardEvent) {
  if ((event.metaKey || event.ctrlKey) && event.key === 's') {
    event.preventDefault()
    handleSave()
  }
}

// ────── Markdown mode actions ──────
function setMdMode(mode: MdViewMode) {
  mdViewMode.value = mode
}
</script>

<template>
  <div class="editor-panel" @keydown="handleKeydown">
    <!-- Tab bar -->
    <div class="editor-tabs">
      <div class="tabs-left">
        <div
          v-for="file in fileStore.openFiles"
          :key="file.path"
          :class="['tab', { active: file.path === fileStore.activeFilePath }]"
          @click="selectTab(file.path)"
        >
          <span class="tab-icon">{{ file.isDiff ? '⇄' : '📄' }}</span>
          <span class="tab-name">
            {{ file.name }}
            <span v-if="file.isDirty" class="dirty-dot">●</span>
          </span>
          <IconButton
            class="tab-close"
            :label="t('editor.closeTab')"
            @click.stop="closeTab(file.path)"
          >×</IconButton>
        </div>
      </div>

      <!-- Markdown mode switcher (shown only when active file is .md) -->
      <div v-if="isMarkdownFile" class="tabs-md-switcher">
        <IconButton
          class="md-mode-btn"
          :active="mdViewMode === 'code'"
          :label="t('editor.mdCode')"
          @click="setMdMode('code')"
        >&lt;/&gt;</IconButton>
        <IconButton
          class="md-mode-btn"
          :active="mdViewMode === 'split'"
          :label="t('editor.mdSplit')"
          @click="setMdMode('split')"
        >⊞</IconButton>
        <IconButton
          class="md-mode-btn"
          :active="mdViewMode === 'preview'"
          :label="t('editor.mdPreview')"
          @click="setMdMode('preview')"
        >👁</IconButton>
      </div>
    </div>

    <!-- Editor content area -->
    <div class="editor-content">
      <!-- No file open -->
      <div v-if="!fileStore.activeFile" class="empty-editor">
        <div class="empty-icon">📝</div>
        <p>{{ t('editor.openFile') }}</p>
      </div>

      <!-- Markdown: preview-only mode -->
      <MarkdownPreview
        v-else-if="isMarkdownFile && mdViewMode === 'preview' && fileStore.activeFile"
        :content="fileStore.activeFile.content"
      />

      <!-- Markdown: split mode (code left + preview right) -->
      <div
        v-else-if="isMarkdownFile && mdViewMode === 'split'"
        class="editor-split-area"
      >
        <div :style="{ width: codePaneWidth }" class="split-pane">
          <!-- Monaco -->
          <div
            ref="editorContainer"
            class="monaco-container"
          />
          <!-- Fallback textarea -->
          <textarea
            v-if="!monacoLoaded || fallbackMode"
            class="fallback-editor"
            :value="fileStore.activeFile?.content"
            :readonly="fileStore.activeFile?.readOnly"
            @input="handleTextareaInput"
            spellcheck="false"
          />
        </div>
        <SplitHandle
          orientation="vertical"
          :label="t('layout.resizeEditorSplit')"
          :value-now="mdSplitResize.size.value"
          :class="{ 'resizing-v': mdSplitResize.isResizing }"
          @resize="onMdSplitResize"
        />
        <div class="split-pane split-preview">
          <MarkdownPreview :content="fileStore.activeFile?.content || ''" />
        </div>
      </div>

      <!-- Normal / code-only mode -->
      <template v-else>
        <!-- Monaco Editor -->
        <div
          v-if="monacoLoaded && !fallbackMode"
          ref="editorContainer"
          class="monaco-container"
        />

        <!-- Fallback textarea -->
        <textarea
          v-else-if="fileStore.activeFile"
          class="fallback-editor"
          :value="fileStore.activeFile.content"
          :readonly="fileStore.activeFile.readOnly"
          @input="handleTextareaInput"
          spellcheck="false"
        />
      </template>
    </div>

    <!-- Bottom status bar -->
    <div class="editor-status">
      <span v-if="fileStore.activeFile">{{ fileStore.activeFile.language.toUpperCase() }}</span>
      <span>UTF-8</span>
      <span v-if="fileStore.activeFile?.readOnly" class="readonly-badge">Read-only</span>
      <span v-if="isMarkdownFile && mdViewMode !== 'code'" class="md-badge">
        MD {{ mdViewMode === 'split' ? 'Split' : 'Preview' }}
      </span>
      <span class="status-right">
        <button v-if="fileStore.activeFile?.isDirty && !fileStore.activeFile?.readOnly" class="save-btn" @click="handleSave">
          Save (⌘S)
        </button>
      </span>
    </div>
  </div>
</template>

<style scoped>
.editor-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.editor-tabs {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
  min-height: 35px;
}

.tabs-left {
  display: flex;
  overflow-x: auto;
  flex: 1;
}

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 12px;
  height: 35px;
  font-size: 12px;
  color: var(--text-muted);
  border-right: 1px solid var(--border);
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
}

.tab:hover {
  color: var(--text-secondary);
}

.tab.active {
  background: var(--bg-primary);
  color: var(--text-primary);
  border-bottom: 2px solid var(--accent);
}

.tab-icon {
  font-size: 11px;
}

.tab-name {
  font-size: 12px;
  position: relative;
}

.dirty-dot {
  color: var(--accent);
  margin-left: 4px;
  font-size: 8px;
}

.tab-close {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 14px;
  padding: 0 2px;
  border-radius: 3px;
  opacity: 0;
}

.tab:hover .tab-close {
  opacity: 1;
}

.tab-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* ────── Markdown mode switcher (tab bar right) ────── */
.tabs-md-switcher {
  display: flex;
  gap: 2px;
  padding: 0 8px;
  flex-shrink: 0;
}

.md-mode-btn {
  background: none;
  border: 1px solid transparent;
  border-radius: 4px;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
  padding: 2px 6px;
  transition: all 0.12s;
}

.md-mode-btn:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
  border-color: var(--border);
}

.md-mode-btn.active {
  color: var(--accent);
  background: var(--bg-hover);
  border-color: var(--accent);
}

/* ────── Editor content ────── */
.editor-content {
  flex: 1;
  overflow: hidden;
  background: var(--bg-primary);
  position: relative;
}

.monaco-container {
  width: 100%;
  height: 100%;
}

.fallback-editor {
  width: 100%;
  height: 100%;
  background: var(--bg-primary);
  color: var(--text-primary);
  border: none;
  padding: 12px 16px;
  font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  resize: none;
  outline: none;
  tab-size: 2;
}

.empty-editor {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-muted);
  gap: 12px;
}

.empty-icon {
  font-size: 48px;
  opacity: 0.3;
}

.empty-editor p {
  font-size: 14px;
}

/* ────── Split-pane layout for md split mode ────── */
.editor-split-area {
  display: flex;
  height: 100%;
  overflow: hidden;
}

.split-pane {
  height: 100%;
  overflow: hidden;
}

.split-pane:first-child {
  min-width: 120px;
  flex-shrink: 0;
}

.split-pane.split-preview {
  flex: 1;
  background: var(--bg-primary);
}

/* Resize handle within split area */
.resize-handle-v {
  width: 1px;
  background: var(--border);
  cursor: col-resize;
  flex-shrink: 0;
  transition: background 0.15s;
}

.resize-handle-v:hover,
.resizing-v {
  background: var(--accent);
  width: 3px;
}

/* ────── Bottom status bar ────── */
.editor-status {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 12px;
  height: 22px;
  background: var(--bg-surface);
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-muted);
}

.unsaved-badge,
.readonly-badge {
  color: var(--accent);
  font-weight: 600;
}

.md-badge {
  color: var(--accent);
  font-style: italic;
}

.status-right {
  margin-left: auto;
}

.save-btn {
  background: none;
  border: 1px solid var(--accent-dim);
  border-radius: 4px;
  color: var(--accent);
  font-size: 10px;
  padding: 1px 6px;
  cursor: pointer;
}

.save-btn:hover {
  background: var(--accent);
  color: var(--accent-foreground);
}
</style>
