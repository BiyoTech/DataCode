<script setup lang="ts">
/**
 * AbiAdhoc — the ad-hoc SQL workbench (the "Data / Ad-hoc" experience).
 *
 * Layout:
 *   ┌────────────┬─────────────────────────────────────┐
 *   │ Data       │ [ tabs ..................... + ]      │
 *   │ Catalog    │ [ Run ▾ | save | format | ... ]      │
 *   │ tree       │ ┌─────────── SQL editor ──────────┐  │
 *   │            │ └──────────────────────────────────┘ │
 *   │            │ ┌────── result grid / status ─────┐  │
 *   └────────────┴─────────────────────────────────────┘
 *
 * Multiple query tabs are supported; each keeps its own SQL text and result.
 * Double-clicking a table in the catalog injects a SELECT template into the
 * active tab. This component owns UI/state only — execution is delegated to the
 * adhoc store.
 */
import { ref, computed, onMounted, watch, nextTick, h } from 'vue'
import { useI18n } from 'vue-i18n'
import { DialogPlugin, MessagePlugin } from 'tdesign-vue-next'
import { extractApiMessage } from '@/utils/notifyError'
import { useAdhocStore, type QueryResult, type CatalogNode } from '@/stores/adhoc'
import { useWorkspaceStore } from '@/stores/workspace'
import AbiCatalogTree from './AbiCatalogTree.vue'
import AbiSqlEditor from './AbiSqlEditor.vue'
import SplitHandle from '@/components/common/SplitHandle.vue'
import IconButton from '@/components/common/IconButton.vue'
import { usePersistedLayoutSize } from '@/composables/usePersistedLayoutSize'

const { t } = useI18n()
const adhoc = useAdhocStore()
const workspaceStore = useWorkspaceStore()

// ─────────── Resizable layout ───────────
// Both panel sizes are persisted to localStorage so the user's preferred
// layout is restored on reload. Bounds are inclusive.
const CATALOG_WIDTH_KEY = 'abi.adhoc.catalog.width'
const RESULT_HEIGHT_KEY = 'abi.adhoc.result.height'
const CATALOG_WIDTH_MIN = 200
const CATALOG_WIDTH_MAX = 500
const CATALOG_WIDTH_DEFAULT = 260
const RESULT_HEIGHT_MIN = 140
const RESULT_HEIGHT_MAX = 600
const RESULT_HEIGHT_DEFAULT = 300

const { size: catalogWidth, applyDelta: applyCatalogDelta } = usePersistedLayoutSize(
  CATALOG_WIDTH_KEY,
  CATALOG_WIDTH_DEFAULT,
  CATALOG_WIDTH_MIN,
  CATALOG_WIDTH_MAX,
)

const { size: resultHeight, setSize: setResultHeight } = usePersistedLayoutSize(
  RESULT_HEIGHT_KEY,
  RESULT_HEIGHT_DEFAULT,
  RESULT_HEIGHT_MIN,
  RESULT_HEIGHT_MAX,
)

function onCatalogResize(delta: number) {
  applyCatalogDelta(delta)
}

function onResultResize(delta: number) {
  setResultHeight(resultHeight.value - delta)
}

// Reference to the active editor instance, used to insert identifiers at the
// cursor when the user clicks "insert" in the catalog.
const editorRef = ref<InstanceType<typeof AbiSqlEditor> | null>(null)

// Resolve the active gateway so SQL execution has a target. fetchGateways
// no-ops when no workspace is selected (e.g. right after the page reload that
// a workspace switch triggers), so it is safe to call before the workspace
// list has loaded; the watcher re-runs it once a workspace is available.
// The catalog tree loads the project models independently (see AbiCatalogTree).
onMounted(() => {
  adhoc.fetchGateways()
})

watch(
  () => workspaceStore.currentWorkspace,
  (ws) => {
    if (!ws) return
    adhoc.fetchGateways()
  },
)

// ─────────── Query tabs ───────────
interface QueryTab {
  id: number
  title: string
  sql: string
  filePath?: string // persisted file path (e.g. "ad-hocs/query1.sql"), undefined for unsaved
  savedContent?: string // last saved content to detect dirty state
  result: QueryResult | null
  loading: boolean
  error: string
}

/** Default name for a new query, precise to the minute. */
function defaultQueryName(): string {
  const d = new Date()
  const yyyy = d.getFullYear()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `query-${yyyy}${mm}${dd}-${hh}${mi}`
}

let tabSeq = 0
function makeTab(opts?: { filePath?: string; sql?: string }): QueryTab {
  tabSeq += 1
  const sql = opts?.sql || ''
  const title = opts?.filePath
    ? (opts.filePath.split('/').pop() || opts.filePath).replace(/\.sql$/i, '')
    : defaultQueryName()
  return {
    id: tabSeq,
    title,
    sql,
    filePath: opts?.filePath,
    savedContent: opts?.filePath !== undefined ? sql : undefined,
    result: null,
    loading: false,
    error: '',
  }
}

const tabs = ref<QueryTab[]>([makeTab()])
const activeId = ref<number>(tabs.value[0].id)

const activeTab = computed<QueryTab>(
  () => tabs.value.find((tb) => tb.id === activeId.value) || tabs.value[0],
)

function addTab() {
  const tb = makeTab()
  tabs.value.push(tb)
  activeId.value = tb.id
}

function closeTab(id: number) {
  const idx = tabs.value.findIndex((tb) => tb.id === id)
  if (idx === -1) return

  const tab = tabs.value[idx]
  // Confirm before closing unsaved tabs
  if (isDirty(tab)) {
    const dialog = DialogPlugin.confirm({
      header: t('abi.adhoc.file.unsaved'),
      body: `"${tab.title}" has unsaved changes. Close without saving?`,
      theme: 'warning',
      confirmBtn: t('common.confirm'),
      cancelBtn: t('common.cancel'),
      onConfirm: () => {
        dialog.hide()
        doCloseTab(idx, id)
      },
    })
    return
  }
  doCloseTab(idx, id)
}

function doCloseTab(idx: number, id: number) {
  tabs.value.splice(idx, 1)
  if (tabs.value.length === 0) {
    addTab()
    return
  }
  if (activeId.value === id) {
    activeId.value = tabs.value[Math.max(0, idx - 1)].id
  }
}

function isDirty(tab: QueryTab): boolean {
  return !tab.filePath || tab.sql !== (tab.savedContent ?? '')
}

// ─────────── Save ───────────
async function saveFile() {
  const tab = activeTab.value
  if (!tab.filePath) {
    // Unsaved tab — prompt for name and create. Pre-fill with the tab's
    // current title (which may be a time-based default or a custom rename)
    // and select-all on focus so the user can type to overwrite.
    // Use a ref so the rendered input value stays in sync with the latest
    // user input even if TDesign re-runs the body function mid-edit.
    const inputValue = ref(tab.title)
    const dialog = DialogPlugin({
      header: t('abi.adhoc.file.save'),
      // Render a real input VNode. Passing an HTML string here would be
      // escaped and shown as literal text, so we must use a render function.
      body: () =>
        h('div', { class: 't-input__wrap' }, [
          h('input', {
            class: 't-input__inner',
            value: inputValue.value,
            placeholder: t('abi.adhoc.file.fileNamePlaceholder'),
            autofocus: true,
            onInput: (e: Event) => {
              inputValue.value = (e.target as HTMLInputElement).value
            },
            onFocus: (e: Event) => {
              ;(e.target as HTMLInputElement).select()
            },
          }),
        ]),
      confirmBtn: t('common.confirm'),
      cancelBtn: t('common.cancel'),
      onConfirm: async () => {
        const name = inputValue.value.trim()
        if (!name) {
          MessagePlugin.warning(t('abi.adhoc.file.invalidName'))
          return
        }
        dialog.hide()
        try {
          // Store will auto-suffix the name on conflict and return the final name
          const { filePath, finalName, renamed } = await adhoc.createAdhocQuery(name)
          tab.filePath = filePath
          tab.title = finalName
          tab.savedContent = tab.sql
          await adhoc.saveAdhocFile(filePath, tab.sql)
          await adhoc.listAdhocFiles()
          if (renamed) {
            MessagePlugin.success(t('abi.adhoc.file.savedAs', { name: finalName }))
          } else {
            MessagePlugin.success(t('abi.adhoc.file.saveSuccess'))
          }
        } catch (e: any) {
          MessagePlugin.error(extractApiMessage(e, t('abi.adhoc.file.saveFailed')))
        }
      },
    })
    return
  }
  try {
    await adhoc.saveAdhocFile(tab.filePath, tab.sql)
    tab.savedContent = tab.sql
    MessagePlugin.success(t('abi.adhoc.file.saveSuccess'))
  } catch (e: any) {
    MessagePlugin.error(extractApiMessage(e, t('abi.adhoc.file.saveFailed')))
  }
}

// ─────────── Open file (called from AbiView nav) ───────────
async function openAdhocFile(filePath: string) {
  // Check if already open
  const existing = tabs.value.find((tb) => tb.filePath === filePath)
  if (existing) {
    activeId.value = existing.id
    return
  }
  try {
    const content = await adhoc.loadAdhocFile(filePath)
    const tab = makeTab({ filePath, sql: content })
    tabs.value.push(tab)
    activeId.value = tab.id
  } catch (e: any) {
    MessagePlugin.error(extractApiMessage(e, 'Failed to open file'))
  }
}

defineExpose({ openAdhocFile })

// ─────────── Inline tab rename (double-click title → input, blur to save) ───────────
// Only one tab can be in rename mode at a time. The input commits on blur
// or Enter; Escape cancels. Names are validated and conflicts are auto-suffixed.
const renamingTabId = ref<number | null>(null)
const renamingValue = ref('')
const renameInputRef = ref<HTMLInputElement | null>(null)

/** Validate a candidate file base name. Returns null on success or an error key. */
function validateTabName(name: string): string | null {
  if (!name) return 'abi.adhoc.file.invalidName'
  if (name.includes('/') || name.includes('\\') || name.includes('\0')) {
    return 'abi.adhoc.file.invalidName'
  }
  return null
}

/** Enter rename mode for a tab. */
function startRenameTab(tab: QueryTab) {
  if (renamingTabId.value === tab.id) return
  renamingTabId.value = tab.id
  renamingValue.value = tab.title
  // Focus + select on next tick once the input is in the DOM
  nextTick(() => {
    renameInputRef.value?.focus()
    renameInputRef.value?.select()
  })
}

/** Cancel rename mode without saving. */
function cancelRenameTab() {
  renamingTabId.value = null
  renamingValue.value = ''
}

/** Commit the rename: validate → optionally disambiguate → persist. */
async function commitRenameTab(tab: QueryTab) {
  const newName = renamingValue.value.trim().replace(/\.sql$/i, '')
  const oldName = tab.title
  cancelRenameTab() // exit edit mode immediately so the UI feels snappy

  if (newName === oldName) return // no-op
  const errKey = validateTabName(newName)
  if (errKey) {
    MessagePlugin.warning(t(errKey))
    return
  }

  // Unsaved tab: just update the in-memory title; save will pick it up.
  if (!tab.filePath) {
    tab.title = newName
    return
  }

  // Saved tab: pick a non-conflicting name and rename the file on disk.
  const { finalName, renamed } = adhoc.findAvailableFileName(newName)
  const oldPath = tab.filePath
  const newPath = `${oldPath.split('/').slice(0, -1).join('/')}/${finalName}.sql`
  try {
    await adhoc.renameAdhocFile(oldPath, newPath)
    tab.title = finalName
    tab.filePath = newPath
    if (renamed) {
      MessagePlugin.success(t('abi.adhoc.file.savedAs', { name: finalName }))
    }
  } catch (e: any) {
    MessagePlugin.error(extractApiMessage(e, t('abi.adhoc.file.saveFailed')))
  }
}

/** Handle keyboard events on the rename input. */
function onRenameKeydown(tab: QueryTab, e: KeyboardEvent) {
  if (e.key === 'Enter') {
    e.preventDefault()
    commitRenameTab(tab)
  } else if (e.key === 'Escape') {
    e.preventDefault()
    cancelRenameTab()
  }
}

// ─────────── Row-limit selector ───────────
const rowLimitOptions = [100, 1000, 10000]
const rowLimit = ref<number>(1000)
const showLimitMenu = ref(false)

function selectLimit(n: number) {
  rowLimit.value = n
  showLimitMenu.value = false
}

// ─────────── Editor / catalog wiring ───────────
function onEditorInput(value: string) {
  activeTab.value.sql = value
}

/** Inject a table SELECT template into the active tab (append or replace). */
function onCatalogOpen(sql: string) {
  const tab = activeTab.value
  tab.sql = tab.sql.trim() ? `${tab.sql.trimEnd()}\n\n${sql}` : sql
}

/** Insert a bare identifier (table FQN or column name) at the editor cursor. */
function onCatalogInsert(identifier: string) {
  editorRef.value?.insertText(identifier)
}

// ─────────── Table preview popup ───────────
const previewOpen = ref(false)
const previewTitle = ref('')
const previewLoading = ref(false)
const previewError = ref('')
const previewResult = ref<QueryResult | null>(null)
const PREVIEW_ROW_LIMIT = 10

/** Open the preview popup and evaluate the first N rows of a table. */
async function onCatalogPreview(node: CatalogNode) {
  previewOpen.value = true
  previewTitle.value = node.fqn || node.name
  previewLoading.value = true
  previewError.value = ''
  previewResult.value = null
  try {
    previewResult.value = await adhoc.previewTable(node, PREVIEW_ROW_LIMIT)
  } catch (e: any) {
    previewError.value = extractApiMessage(e, 'Preview failed')
  } finally {
    previewLoading.value = false
  }
}

function closePreview() {
  previewOpen.value = false
}

// ─────────── Run ───────────
async function runQuery() {
  const tab = activeTab.value
  const sql = tab.sql.trim()
  if (!sql || tab.loading) return

  tab.loading = true
  tab.error = ''
  try {
    tab.result = await adhoc.runQuery(sql, rowLimit.value)
  } catch (e: any) {
    tab.error = extractApiMessage(e, 'Query failed')
    tab.result = null
  } finally {
    tab.loading = false
  }
}
</script>

<template>
  <div class="abi-adhoc">
    <!-- Left: data catalog -->
    <aside class="adhoc-catalog" :style="{ width: catalogWidth + 'px' }">
      <AbiCatalogTree
        @open="onCatalogOpen"
        @insert="onCatalogInsert"
        @preview="onCatalogPreview"
      />
    </aside>

    <!-- Resize handle between the catalog tree and the editor/result area -->
    <SplitHandle
      orientation="vertical"
      :label="t('abi.adhoc.catalog.resize')"
      @resize="onCatalogResize"
    />

    <!-- Right: editor + results -->
    <section class="adhoc-main">
      <!-- Tab bar -->
      <div class="tab-bar">
        <div class="tab-list">
          <div
            v-for="tb in tabs"
            :key="tb.id"
            class="tab"
            :class="{ active: tb.id === activeId, renaming: renamingTabId === tb.id }"
            @click="activeId = tb.id"
          >
            <t-icon name="file" size="14px" />
            <span v-if="isDirty(tb) && renamingTabId !== tb.id" class="tab-dirty">*</span>
            <input
              v-if="renamingTabId === tb.id"
              ref="renameInputRef"
              v-model="renamingValue"
              class="tab-rename-input"
              :title="t('abi.adhoc.file.rename')"
              @blur="commitRenameTab(tb)"
              @keydown="onRenameKeydown(tb, $event)"
              @click.stop
            />
            <span
              v-else
              class="tab-title"
              :title="tb.title"
              @dblclick.stop="startRenameTab(tb)"
            >{{ tb.title }}</span>
            <!--
              Wrap t-icon in a native span: t-icon accepts onClick as a prop
              but does not emit a `click` event, so a direct @click would
              never fire. @click.stop prevents the tab-activate handler on
              the outer .tab from firing.
            -->
            <IconButton
              v-if="tabs.length > 1 && renamingTabId !== tb.id"
              class="tab-close"
              :label="t('common.close')"
              @click.stop="closeTab(tb.id)"
            >
              <t-icon name="close" size="12px" />
            </IconButton>
          </div>
          <IconButton class="tab-add" :label="t('abi.adhoc.tab.new')" @click="addTab">
            <t-icon name="add" size="16px" />
          </IconButton>
        </div>
      </div>

      <!-- Toolbar -->
      <div class="toolbar">
        <!-- Run + row-limit split control -->
        <div class="run-group">
          <button class="run-btn" :disabled="activeTab.loading" @click="runQuery">
            <t-icon v-if="activeTab.loading" name="loading" size="14px" class="spinning" />
            <t-icon v-else name="play" size="14px" />
            <span>{{ t('abi.adhoc.toolbar.run') }}</span>
          </button>
          <div class="limit-switcher">
            <button class="limit-trigger" @click="showLimitMenu = !showLimitMenu">
              <span>{{ t('abi.adhoc.toolbar.allRows') }}({{ rowLimit }})</span>
              <t-icon name="chevron-down" size="12px" />
            </button>
            <div v-if="showLimitMenu" class="limit-menu">
              <button
                v-for="n in rowLimitOptions"
                :key="n"
                class="limit-option"
                :class="{ active: n === rowLimit }"
                @click="selectLimit(n)"
              >
                {{ t('abi.adhoc.toolbar.allRows') }}({{ n }})
              </button>
            </div>
            <div v-if="showLimitMenu" class="limit-backdrop" @click="showLimitMenu = false" />
          </div>
        </div>

        <!-- Placeholder actions (framework only) -->
        <div class="toolbar-actions">
          <IconButton class="tool-btn" :label="t('abi.adhoc.file.save')" @click="saveFile">
            <t-icon name="save" size="16px" />
          </IconButton>
          <IconButton class="tool-btn" :label="t('abi.adhoc.toolbar.format')" disabled>
            <t-icon name="code" size="16px" />
          </IconButton>
        </div>
      </div>

      <!-- SQL editor -->
      <div class="editor-pane">
        <AbiSqlEditor
          :key="activeTab.id"
          ref="editorRef"
          :model-value="activeTab.sql"
          @update:model-value="onEditorInput"
          @run="runQuery"
        />
      </div>

      <!-- Resize handle between editor and result area -->
      <SplitHandle
        orientation="horizontal"
        :label="t('abi.adhoc.result.resize')"
        @resize="onResultResize"
      />

      <!-- Result pane -->
      <div class="result-pane" :style="{ height: resultHeight + 'px' }">
        <div class="result-status">
          <template v-if="activeTab.loading">
            <t-icon name="loading" size="14px" class="spinning" />
            <span>{{ t('common.loading') }}</span>
          </template>
          <template v-else-if="activeTab.error">
            <t-icon name="error-circle" size="14px" class="error-icon" />
            <span class="error-text">{{ activeTab.error }}</span>
          </template>
          <template v-else-if="activeTab.result">
            <span>{{ activeTab.result.totalRows }} {{ t('abi.adhoc.result.rows') }}</span>
            <span class="status-sep">·</span>
            <span>{{ activeTab.result.execTimeMs }} ms</span>
          </template>
          <template v-else>
            <span class="result-hint">{{ t('abi.adhoc.result.emptyHint') }}</span>
          </template>
        </div>

        <div v-if="activeTab.result && activeTab.result.columns.length" class="result-table-wrap">
          <table class="result-table">
            <thead>
              <tr>
                <th class="row-idx">#</th>
                <th v-for="col in activeTab.result.columns" :key="col">{{ col }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, rIdx) in activeTab.result.rows" :key="rIdx">
                <td class="row-idx">{{ rIdx + 1 }}</td>
                <td v-for="(cell, cIdx) in row" :key="cIdx">
                  {{ cell === '' || cell == null ? 'NULL' : cell }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- Table preview popup (first N rows via model evaluate) -->
    <div v-if="previewOpen" class="preview-overlay" @click.self="closePreview">
      <div class="preview-dialog">
        <div class="preview-header">
          <div class="preview-title">
            <t-icon name="browse" size="16px" />
            <span>{{ previewTitle }}</span>
            <span class="preview-badge">{{ PREVIEW_ROW_LIMIT }} {{ t('abi.adhoc.result.rows') }}</span>
          </div>
          <button class="preview-close" @click="closePreview">
            <t-icon name="close" size="16px" />
          </button>
        </div>

        <div class="preview-body">
          <div v-if="previewLoading" class="preview-status">
            <t-icon name="loading" size="16px" class="spinning" />
            <span>{{ t('common.loading') }}</span>
          </div>
          <div v-else-if="previewError" class="preview-status error">
            <t-icon name="error-circle" size="16px" />
            <span>{{ previewError }}</span>
          </div>
          <div
            v-else-if="previewResult && previewResult.columns.length"
            class="result-table-wrap"
          >
            <table class="result-table">
              <thead>
                <tr>
                  <th class="row-idx">#</th>
                  <th v-for="col in previewResult.columns" :key="col">{{ col }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, rIdx) in previewResult.rows" :key="rIdx">
                  <td class="row-idx">{{ rIdx + 1 }}</td>
                  <td v-for="(cell, cIdx) in row" :key="cIdx">
                    {{ cell === '' || cell == null ? 'NULL' : cell }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="preview-status">
            <span>{{ t('abi.adhoc.result.emptyHint') }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.abi-adhoc {
  display: flex;
  height: 100%;
  overflow: hidden;
  background: var(--bg-primary);
}

/* Left catalog panel (width is set inline by the resize logic) */
.adhoc-catalog {
  flex-shrink: 0;
  border-right: 1px solid var(--border);
  overflow: hidden;
}

/* Right main area */
.adhoc-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* Tab bar */
.tab-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 40px;
  padding: 0 8px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-secondary);
}

.tab-list {
  display: flex;
  align-items: center;
  gap: 4px;
  overflow-x: auto;
  scrollbar-width: none;
}

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: none;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}

.tab:hover {
  background: var(--bg-hover);
}

.tab.active {
  background: var(--bg-primary);
  border-color: var(--border);
  color: var(--text-primary);
}

.tab.renaming {
  cursor: text;
  background: var(--bg-primary);
  border-color: var(--accent);
}

.tab-dirty {
  color: var(--td-warning-color, #ed7b2f);
  font-weight: 700;
  margin-right: -2px;
}

.tab-title {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tab-rename-input {
  width: 140px;
  height: 22px;
  padding: 0 6px;
  border: none;
  border-radius: 4px;
  background: var(--bg-hover);
  color: var(--text-primary);
  font-size: 13px;
  font-family: inherit;
  outline: none;
}

.tab-rename-input:focus {
  background: var(--bg-primary);
  box-shadow: inset 0 0 0 1px var(--accent);
}

.tab-close {
  color: var(--text-muted);
  border-radius: 4px;
}

.tab-close:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.tab-add {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 6px;
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
}

.tab-add:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* Toolbar */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 40px;
  padding: 0 12px;
  border-bottom: 1px solid var(--border);
}

.run-group {
  display: flex;
  align-items: center;
}

.run-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 12px;
  border: none;
  border-radius: 6px 0 0 6px;
  background: var(--accent);
  color: var(--accent-foreground, #fff);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.run-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.limit-switcher {
  position: relative;
}

.limit-trigger {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 28px;
  padding: 0 10px;
  border: none;
  border-left: 1px solid rgba(255, 255, 255, 0.25);
  border-radius: 0 6px 6px 0;
  background: var(--accent);
  color: var(--accent-foreground, #fff);
  font-size: 12px;
  cursor: pointer;
}

.limit-menu {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  min-width: 140px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-float-lg);
  padding: 4px;
  z-index: 20;
}

.limit-option {
  display: block;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: 6px;
  background: none;
  color: var(--text-primary);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}

.limit-option:hover {
  background: var(--bg-hover);
}

.limit-option.active {
  color: var(--accent);
  font-weight: 600;
}

.limit-backdrop {
  position: fixed;
  inset: 0;
  z-index: 10;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.tool-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 6px;
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
}

.tool-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.tool-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Editor pane (flex-grow fills the space left by the result pane) */
.editor-pane {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}

/* Result pane (height is set inline by the resize logic) */
.result-pane {
  flex-shrink: 0;
  min-height: 140px;
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--border);
  background: var(--bg-primary);
}

.result-status {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-secondary);
  flex-shrink: 0;
}

.status-sep {
  color: var(--text-muted);
}

.result-hint {
  color: var(--text-muted);
}

.error-icon {
  color: var(--td-error-color);
}

.error-text {
  color: var(--td-error-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result-table-wrap {
  flex: 1;
  overflow: auto;
}

.result-table {
  border-collapse: collapse;
  font-size: 12px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  white-space: nowrap;
}

.result-table th {
  position: sticky;
  top: 0;
  background: var(--bg-secondary);
  font-weight: 600;
  color: var(--text-primary);
  text-align: left;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
}

.result-table td {
  padding: 5px 12px;
  border-bottom: 1px solid var(--border);
  border-right: 1px solid var(--border);
  color: var(--text-primary);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.result-table .row-idx {
  color: var(--text-muted);
  text-align: right;
  background: var(--bg-secondary);
}

.result-table tr:hover td {
  background: var(--bg-hover);
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

/* Table preview popup */
.preview-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
}

.preview-dialog {
  display: flex;
  flex-direction: column;
  width: min(80vw, 900px);
  max-height: 70vh;
  border-radius: 12px;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-float-lg);
  overflow: hidden;
}

.preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-secondary);
}

.preview-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.preview-badge {
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--bg-hover);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 500;
  font-family: inherit;
}

.preview-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 6px;
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
}

.preview-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.preview-body {
  flex: 1;
  overflow: auto;
  display: flex;
  flex-direction: column;
}

.preview-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px;
  font-size: 13px;
  color: var(--text-secondary);
}

.preview-status.error {
  color: var(--td-error-color);
}
</style>
