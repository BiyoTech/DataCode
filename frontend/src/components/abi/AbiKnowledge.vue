<script setup lang="ts">
/**
 * AbiKnowledge — business-knowledge document management.
 *
 * Supports creating Markdown documents, uploading files, and browsing
 * documents stored under {workspace}/knowledge/.
 * Content is fetched on demand and shown in a preview panel.
 */
import { ref, h, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { DialogPlugin, MessagePlugin } from 'tdesign-vue-next'
import { extractApiMessage } from '@/utils/notifyError'
import { useKnowledgeStore } from '@/stores/knowledge'
import type { KnowledgeDocument, KnowledgeContentResponse } from '@/types/knowledge'

const { t } = useI18n()
const store = useKnowledgeStore()

// ─────────── Local state ───────────

const searchQuery = ref('')
const loadingContent = ref(false)
const previewContent = ref<KnowledgeContentResponse | null>(null)

// ─────────── Lifecycle ───────────

onMounted(() => {
  store.fetchDocuments()
})

// Re-fetch when search changes (with debounce)
let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(searchQuery, (val) => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    store.fetchDocuments(val)
  }, 300)
})

// ─────────── Computed displayed documents ───────────

const displayedDocuments = () => {
  if (!searchQuery.value) return store.documents
  const q = searchQuery.value.toLowerCase()
  return store.documents.filter(
    (d) =>
      d.title.toLowerCase().includes(q) ||
      (d.tags || '').toLowerCase().includes(q),
  )
}

// ─────────── Actions ───────────

/** Format file size for display. */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Format a UTC ISO timestamp to a relative label. */
function relativeTime(iso: string): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return t('common.justNow') || 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  const months = Math.floor(days / 30)
  return `${months}mo ago`
}

/** Get icon name for content type. */
function typeIcon(contentType: string): string {
  const map: Record<string, string> = {
    markdown: 'file-1',
    text: 'file-text',
    pdf: 'file-pdf',
    csv: 'chart-line',
    json: 'code',
    yaml: 'file-setting',
  }
  return map[contentType] || 'file'
}

/** Check if content type is previewable (text-based). */
function isPreviewable(contentType: string): boolean {
  return ['markdown', 'text', 'json', 'yaml', 'csv'].includes(contentType)
}

// ─────────── Create Document Dialog ───────────

function openCreateDialog() {
  const title = ref('')
  const content = ref('')
  const dialog = DialogPlugin({
    header: t('abi.knowledge.createTitle'),
    width: '600px',
    body: () =>
      h('div', { class: 'create-dialog' }, [
        h('div', { class: 't-form__item' }, [
          h('label', { class: 't-form__label' }, t('abi.knowledge.docTitle')),
          h('input', {
            class: 't-input__inner',
            value: title.value,
            placeholder: t('abi.knowledge.titlePlaceholder'),
            autofocus: true,
            onInput: (e: Event) => {
              title.value = (e.target as HTMLInputElement).value
            },
          }),
        ]),
        h('div', { class: 't-form__item' }, [
          h('label', { class: 't-form__label' }, t('abi.knowledge.content')),
          h('textarea', {
            class: 't-textarea__inner',
            value: content.value,
            placeholder: t('abi.knowledge.contentPlaceholder'),
            rows: 12,
            style: 'resize: vertical; min-height: 200px; line-height: 1.6;',
            onInput: (e: Event) => {
              content.value = (e.target as HTMLTextAreaElement).value
            },
          }),
        ]),
      ]),
    confirmBtn: t('common.confirm'),
    cancelBtn: t('common.cancel'),
    onConfirm: async () => {
      const tVal = title.value.trim()
      if (!tVal) {
        MessagePlugin.warning(t('abi.knowledge.titlePlaceholder'))
        return
      }
      dialog.hide()
      try {
        await store.createDocument(tVal, content.value)
        MessagePlugin.success(t('abi.knowledge.createSuccess'))
      } catch (e: any) {
        MessagePlugin.error(extractApiMessage(e, t('abi.knowledge.createFailed')))
      }
    },
  })
}

// ─────────── Upload File ───────────

const fileInputRef = ref<HTMLInputElement | null>(null)

function openUploadDialog() {
  fileInputRef.value?.click()
}

async function onFileSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  try {
    await store.uploadFile(file)
    MessagePlugin.success(t('abi.knowledge.uploadSuccess'))
  } catch (err: any) {
    MessagePlugin.error(extractApiMessage(err, t('abi.knowledge.uploadFailed')))
  } finally {
    // Reset so the same file can be uploaded again
    input.value = ''
  }
}

// ─────────── Preview Document ───────────

async function openPreview(doc: KnowledgeDocument) {
  if (!isPreviewable(doc.content_type)) {
    MessagePlugin.warning(`Cannot preview "${doc.content_type}" files`)
    return
  }
  loadingContent.value = true
  previewContent.value = null
  try {
    const data = await store.readDocumentContent(doc.id)
    previewContent.value = data
  } catch (e: any) {
    MessagePlugin.error(extractApiMessage(e, 'Failed to load content'))
  } finally {
    loadingContent.value = false
  }
}

function closePreview() {
  previewContent.value = null
}

// ─────────── Delete Document ───────────

function deleteDocument(doc: KnowledgeDocument) {
  const dialog = DialogPlugin.confirm({
    header: t('abi.knowledge.deleteConfirm', { title: doc.title }),
    theme: 'warning',
    confirmBtn: t('common.confirm'),
    cancelBtn: t('common.cancel'),
    onConfirm: async () => {
      dialog.hide()
      try {
        await store.deleteDocument(doc.id)
        if (previewContent.value?.id === doc.id) {
          previewContent.value = null
        }
        MessagePlugin.success(t('common.deleted'))
      } catch (e: any) {
        MessagePlugin.error(extractApiMessage(e, 'Delete failed'))
      }
    },
  })
}
</script>

<template>
  <div class="abik">
    <!-- Hidden file input for upload -->
    <input
      ref="fileInputRef"
      type="file"
      accept=".md,.txt,.pdf,.csv,.json,.yaml,.yml"
      style="display: none"
      @change="onFileSelected"
    />

    <!-- Toolbar -->
    <div class="abik-toolbar">
      <h2 class="abik-title">{{ t('abi.knowledge.title') }}</h2>
      <div class="abik-toolbar-actions">
        <button class="abik-btn abik-btn--primary" @click="openCreateDialog">
          <t-icon name="add" size="16px" />
          <span>{{ t('abi.knowledge.create') }}</span>
        </button>
        <button class="abik-btn abik-btn--outline" @click="openUploadDialog">
          <t-icon name="upload" size="16px" />
          <span>{{ t('abi.knowledge.upload') }}</span>
        </button>
      </div>
    </div>

    <!-- Search bar -->
    <div class="abik-search">
      <t-icon name="search" size="14px" class="abik-search-icon" />
      <input
        v-model="searchQuery"
        class="abik-search-input"
        :placeholder="t('abi.knowledge.searchPlaceholder')"
      />
    </div>

    <!-- Loading state -->
    <div v-if="store.isLoading" class="abik-state">
      <t-icon name="loading" size="24px" class="spinning" />
      <span>{{ t('common.loading') }}</span>
    </div>

    <!-- Error state -->
    <div v-else-if="store.error" class="abik-state abik-state--error">
      <t-icon name="error-circle" size="24px" />
      <span>{{ store.error }}</span>
      <button class="abik-btn abik-btn--outline" @click="store.fetchDocuments()">
        {{ t('common.retry') }}
      </button>
    </div>

    <!-- Empty state -->
    <div v-else-if="displayedDocuments().length === 0" class="abik-state">
      <template v-if="searchQuery">
        <t-icon name="search" size="24px" />
        <span>{{ t('common.noResult') }}</span>
      </template>
      <template v-else>
        <t-icon name="book" size="32px" />
        <span class="abik-empty-text">{{ t('abi.knowledge.empty') }}</span>
        <div class="abik-empty-actions">
          <button class="abik-btn abik-btn--primary" @click="openCreateDialog">
            <t-icon name="add" size="14px" />
            {{ t('abi.knowledge.create') }}
          </button>
          <button class="abik-btn abik-btn--outline" @click="openUploadDialog">
            <t-icon name="upload" size="14px" />
            {{ t('abi.knowledge.upload') }}
          </button>
        </div>
      </template>
    </div>

    <!-- Document cards -->
    <div v-else class="abik-list">
      <div
        v-for="doc in displayedDocuments()"
        :key="doc.id"
        class="abik-card"
        :class="{ 'abik-card--active': previewContent?.id === doc.id }"
        @click="openPreview(doc)"
      >
        <div class="abik-card-icon">
          <t-icon :name="typeIcon(doc.content_type)" size="22px" />
        </div>
        <div class="abik-card-body">
          <div class="abik-card-title">{{ doc.title }}</div>
          <div class="abik-card-meta">
            <span class="abik-card-type">{{ doc.content_type }}</span>
            <span class="abik-card-sep">·</span>
            <span>{{ formatSize(doc.file_size) }}</span>
            <span class="abik-card-sep">·</span>
            <span>{{ relativeTime(doc.updated_at || doc.created_at) }}</span>
          </div>
          <div v-if="doc.tags" class="abik-card-tags">
            <span
              v-for="tag in (doc.tags || '').split(',').filter(Boolean)"
              :key="tag"
              class="abik-tag"
            >{{ tag.trim() }}</span>
          </div>
        </div>
        <div class="abik-card-actions">
          <button
            class="abik-action-btn"
            :title="t('abi.knowledge.viewContent')"
            :disabled="!isPreviewable(doc.content_type)"
            @click.stop="openPreview(doc)"
          >
            <t-icon name="browse" size="15px" />
          </button>
          <button
            class="abik-action-btn abik-action-btn--danger"
            :title="t('abi.knowledge.delete')"
            @click.stop="deleteDocument(doc)"
          >
            <t-icon name="delete" size="15px" />
          </button>
        </div>
      </div>
    </div>

    <!-- Preview panel -->
    <div v-if="previewContent || loadingContent" class="abik-preview">
      <div class="abik-preview-header">
        <t-icon name="browse" size="16px" />
        <span class="abik-preview-title">{{ previewContent?.title || '...' }}</span>
        <span class="abik-preview-type">{{ previewContent?.content_type || '' }}</span>
        <div class="abik-preview-spacer" />
        <button class="abik-action-btn" :title="t('abi.knowledge.close')" @click="closePreview">
          <t-icon name="close" size="16px" />
        </button>
      </div>
      <div class="abik-preview-body">
        <t-icon v-if="loadingContent" name="loading" size="20px" class="spinning" />
        <pre v-else class="abik-preview-content">{{ previewContent?.content }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.abik {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 20px 24px;
  overflow: hidden;
  background: var(--bg-primary);
}

/* ── Toolbar ── */
.abik-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-shrink: 0;
}

.abik-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0;
}

.abik-toolbar-actions {
  display: flex;
  gap: 8px;
}

/* ── Buttons ── */
.abik-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background var(--transition-base), color var(--transition-base), box-shadow var(--transition-base);
}

.abik-btn--primary {
  background: var(--accent);
  color: #fff;
}

.abik-btn--primary:hover {
  filter: brightness(1.1);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
}

.abik-btn--outline {
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border);
}

.abik-btn--outline:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* ── Search ── */
.abik-search {
  position: relative;
  margin-bottom: 14px;
  flex-shrink: 0;
}

.abik-search-icon {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-muted);
}

.abik-search-input {
  width: 100%;
  padding: 9px 12px 9px 34px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-secondary);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  transition: border-color var(--transition-base);
}

.abik-search-input::placeholder {
  color: var(--text-muted);
}

.abik-search-input:focus {
  border-color: var(--accent);
}

/* ── State (loading / error / empty) ── */
.abik-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  flex: 1;
  color: var(--text-muted);
  font-size: 13px;
}

.abik-state--error {
  color: var(--danger, #e34d59);
}

.abik-empty-text {
  max-width: 280px;
  text-align: center;
  font-size: 14px;
  color: var(--text-secondary);
}

.abik-empty-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

/* ── Document list ── */
.abik-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}

/* ── Document card ── */
.abik-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg-secondary);
  cursor: pointer;
  transition: background var(--transition-base), border-color var(--transition-base);
}

.abik-card:hover {
  background: var(--bg-hover);
  border-color: var(--accent);
}

.abik-card--active {
  border-color: var(--accent);
  background: var(--bg-hover);
}

.abik-card-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 8px;
  background: var(--bg-primary);
  color: var(--accent);
  flex-shrink: 0;
}

.abik-card-body {
  flex: 1;
  min-width: 0;
}

.abik-card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.abik-card-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-muted);
}

.abik-card-type {
  text-transform: capitalize;
}

.abik-card-sep {
  color: var(--border);
}

.abik-card-tags {
  display: flex;
  gap: 4px;
  margin-top: 4px;
  flex-wrap: wrap;
}

.abik-tag {
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--bg-primary);
  color: var(--text-muted);
  font-size: 11px;
  border: 1px solid var(--border);
}

.abik-card-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
  opacity: 0;
  transition: opacity var(--transition-base);
}

.abik-card:hover .abik-card-actions {
  opacity: 1;
}

.abik-action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: background var(--transition-base), color var(--transition-base);
}

.abik-action-btn:hover:not(:disabled) {
  background: var(--bg-primary);
  color: var(--text-primary);
}

.abik-action-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.abik-action-btn--danger:hover:not(:disabled) {
  color: var(--danger, #e34d59);
}

/* ── Preview panel ── */
.abik-preview {
  display: flex;
  flex-direction: column;
  margin-top: 12px;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
  max-height: 40%;
  overflow: hidden;
}

.abik-preview-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 0 8px;
  flex-shrink: 0;
}

.abik-preview-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.abik-preview-type {
  font-size: 11px;
  color: var(--text-muted);
  text-transform: capitalize;
  padding: 1px 6px;
  border-radius: 10px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
}

.abik-preview-spacer {
  flex: 1;
}

.abik-preview-body {
  flex: 1;
  overflow-y: auto;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 12px 0;
}

.abik-preview-content {
  width: 100%;
  margin: 0;
  padding: 14px;
  border-radius: 8px;
  background: var(--bg-secondary);
  color: var(--text-primary);
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1px solid var(--border);
  font-family: var(--font-mono, 'SF Mono', 'Fira Code', monospace);
}

/* ── Create dialog styles ── */
:deep(.create-dialog) {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

:deep(.t-form__item) {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

:deep(.t-form__label) {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
}

:deep(.t-input__inner),
:deep(.t-textarea__inner) {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  font-family: inherit;
}

:deep(.t-input__inner:focus),
:deep(.t-textarea__inner:focus) {
  border-color: var(--accent);
}

:deep(.t-textarea__inner) {
  resize: vertical;
  min-height: 200px;
  line-height: 1.6;
}

/* ── Spinner ── */
.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
