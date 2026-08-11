<script setup lang="ts">
/**
 * AbiSqlEditor — a lightweight Monaco-backed SQL editor with v-model support.
 *
 * Reuses the Monaco integration pattern from EditorPanel.vue (dynamic import,
 * worker setup, dispose on unmount) but is scoped to a single SQL document.
 * Theme follows the app's resolved light/dark mode. Cmd/Ctrl+Enter emits `run`
 * so the workbench can execute the current statement.
 *
 * Falls back to a plain textarea if Monaco fails to load.
 */
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { useThemeStore } from '@/stores/theme'

const props = defineProps<{
  modelValue: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'run'): void
}>()

const themeStore = useThemeStore()

const editorContainer = ref<HTMLDivElement | null>(null)
const monacoLoaded = ref(false)
const fallbackMode = ref(false)
let monaco: any = null
let editor: any = null
// Guard against feeding external updates back into Monaco as edits.
let applyingExternal = false

function monacoTheme(): string {
  return themeStore.isDark ? 'vs-dark' : 'vs'
}

onMounted(async () => {
  try {
    // Configure Monaco workers once (idempotent across editor instances).
    if (!self.MonacoEnvironment) {
      self.MonacoEnvironment = {
        getWorker() {
          return new Worker(
            new URL('monaco-editor/esm/vs/editor/editor.worker.js', import.meta.url),
            { type: 'module' },
          )
        },
      }
    }

    monaco = await import('monaco-editor')
    monacoLoaded.value = true
    createEditor()
  } catch {
    fallbackMode.value = true
  }
})

onBeforeUnmount(() => {
  if (editor) {
    editor.dispose()
    editor = null
  }
})

function createEditor() {
  if (!editorContainer.value || !monaco) return

  editor = monaco.editor.create(editorContainer.value, {
    value: props.modelValue || '',
    language: 'sql',
    theme: monacoTheme(),
    fontSize: 13,
    fontFamily: "'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, monospace",
    minimap: { enabled: false },
    automaticLayout: true,
    scrollBeyondLastLine: false,
    tabSize: 2,
    lineNumbers: 'on',
    renderWhitespace: 'selection',
    scrollbar: { alwaysConsumeMouseWheel: false },
  })

  // Propagate edits to the parent through v-model.
  editor.onDidChangeModelContent(() => {
    if (applyingExternal) return
    emit('update:modelValue', editor.getValue())
  })

  // Cmd/Ctrl + Enter runs the query.
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
    emit('run')
  })
}

// Reflect external model changes (e.g. inserting a table SELECT template).
watch(
  () => props.modelValue,
  (val) => {
    if (editor && val !== editor.getValue()) {
      applyingExternal = true
      editor.setValue(val || '')
      applyingExternal = false
    }
  },
)

// Follow app theme switches.
watch(
  () => themeStore.isDark,
  () => {
    if (monaco) monaco.editor.setTheme(monacoTheme())
  },
)

/**
 * Insert text at the current cursor / selection and refocus the editor.
 * Falls back to appending through v-model when Monaco is unavailable.
 */
function insertText(text: string) {
  if (editor && monaco) {
    const selection = editor.getSelection()
    editor.executeEdits('catalog-insert', [
      { range: selection, text, forceMoveMarkers: true },
    ])
    editor.focus()
    return
  }
  const next = props.modelValue ? `${props.modelValue}${text}` : text
  emit('update:modelValue', next)
}

defineExpose({ insertText })
</script>

<template>
  <div class="sql-editor">
    <div v-show="monacoLoaded && !fallbackMode" ref="editorContainer" class="editor-container" />
    <textarea
      v-if="fallbackMode"
      class="fallback-editor"
      :value="modelValue"
      spellcheck="false"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    />
  </div>
</template>

<style scoped>
.sql-editor {
  width: 100%;
  height: 100%;
  overflow: hidden;
}

.editor-container {
  width: 100%;
  height: 100%;
}

.fallback-editor {
  width: 100%;
  height: 100%;
  border: none;
  outline: none;
  resize: none;
  padding: 8px 12px;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 13px;
}
</style>
