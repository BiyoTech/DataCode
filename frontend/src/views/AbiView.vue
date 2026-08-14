<script setup lang="ts">
/**
 * AbiView — shell for the A-BI (AI-native BI) module.
 *
 * A-BI is a self-contained module with its own left navigation and four
 * sections:
 *   - adhoc     (Ad-hoc)     : SQL workbench for ad-hoc queries (expandable file list)
 *   - dashboard (Dashboard)  : dashboards built from analysis results
 *   - knowledge (业务知识)    : business-knowledge management
 *   - agent     (Agent会话)  : agent conversation history / interactive analysis
 *
 * This component only owns the layout/navigation framework; each section is a
 * dedicated child component so they can evolve independently.
 */
import { ref, computed, h, onMounted, watch, defineAsyncComponent } from 'vue'
import { useI18n } from 'vue-i18n'
import { DialogPlugin, MessagePlugin } from 'tdesign-vue-next'
import { extractApiMessage } from '@/utils/notifyError'
import { useAdhocStore, type AdhocFile } from '@/stores/adhoc'
import { useWorkspaceStore } from '@/stores/workspace'
import AbiAdhoc from '@/components/abi/AbiAdhoc.vue'
import { usePersistedLayoutSize } from '@/composables/usePersistedLayoutSize'
import SplitHandle from '@/components/common/SplitHandle.vue'

const AbiDashboard = defineAsyncComponent(() => import('@/components/abi/AbiDashboard.vue'))
const AbiKnowledge = defineAsyncComponent(() => import('@/components/abi/AbiKnowledge.vue'))
const AbiAgentChat = defineAsyncComponent(() => import('@/components/abi/AbiAgentChat.vue'))

const { t } = useI18n()
const adhoc = useAdhocStore()
const workspaceStore = useWorkspaceStore()

type AbiSection = 'adhoc' | 'dashboard' | 'knowledge' | 'agent'

// Currently active section; defaults to the ad-hoc SQL workbench.
const activeSection = ref<AbiSection>('adhoc')

// Whether the ad-hoc file list is expanded in the nav.
const adhocExpanded = ref(true)

// Reference to the AbiAdhoc child component for opening files.
const adhocRef = ref<InstanceType<typeof AbiAdhoc> | null>(null)

// ─────────── Resizable layout (left nav) ───────────
// Nav width is persisted to localStorage so the user's preferred size is
// restored on reload. Bounds are inclusive.
const NAV_WIDTH_KEY = 'abi.nav.width'
const NAV_WIDTH_MIN = 180
const NAV_WIDTH_MAX = 400
const NAV_WIDTH_DEFAULT = 200

const { size: navWidth, applyDelta: applyNavDelta } = usePersistedLayoutSize(
  NAV_WIDTH_KEY,
  NAV_WIDTH_DEFAULT,
  NAV_WIDTH_MIN,
  NAV_WIDTH_MAX,
)

function onNavResize(delta: number) {
  applyNavDelta(delta)
}

// Nav items excluding ad-hoc (which gets special expandable treatment).
const otherNavItems = computed(() => [
  { key: 'agent' as const, label: t('abi.nav.agent'), icon: 'chat' },
  { key: 'dashboard' as const, label: t('abi.nav.dashboard'), icon: 'dashboard' },
  { key: 'knowledge' as const, label: t('abi.nav.knowledge'), icon: 'book' },
])

function selectSection(key: AbiSection) {
  activeSection.value = key
  if (key !== 'adhoc') {
    adhocExpanded.value = false
  }
}

// The ad-hoc file list is expanded by default, so populate it on load. This may
// run before the workspace list has been fetched (e.g. right after the full
// page reload that switching workspaces triggers); listAdhocFiles no-ops when
// no workspace is selected, and the watcher below reloads once one is available.
onMounted(() => {
  if (adhocExpanded.value) adhoc.listAdhocFiles()
})

watch(
  () => workspaceStore.currentWorkspace,
  (ws) => {
    if (!ws) return
    if (adhocExpanded.value) adhoc.listAdhocFiles()
  },
)

/** Toggle the ad-hoc section: expand nav + show workbench. */
function toggleAdhoc() {
  if (activeSection.value === 'adhoc') {
    // Already on ad-hoc: toggle file list only
    adhocExpanded.value = !adhocExpanded.value
    if (adhocExpanded.value) {
      adhoc.listAdhocFiles()
    }
  } else {
    activeSection.value = 'adhoc'
    adhocExpanded.value = true
    adhoc.listAdhocFiles()
  }
}

/** Open an ad-hoc file in the editor. */
async function openFile(filePath: string) {
  await adhocRef.value?.openAdhocFile(filePath)
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

/** Create a new ad-hoc query via dialog. */
async function createQuery() {
  // Use a ref so the rendered input value stays in sync with the latest
  // user input even if TDesign re-runs the body function mid-edit.
  const inputValue = ref(defaultQueryName())
  const dialog = DialogPlugin({
    header: t('abi.adhoc.file.newQueryTitle'),
    // Render a real input VNode; an HTML string would be escaped and shown as text.
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
        // Store auto-suffixes on conflict and refreshes the file list
        const { filePath, finalName, renamed } = await adhoc.createAdhocQuery(name)
        // Switch to ad-hoc section and open the new file
        activeSection.value = 'adhoc'
        adhocExpanded.value = true
        await adhocRef.value?.openAdhocFile(filePath)
        if (renamed) {
          MessagePlugin.success(t('abi.adhoc.file.savedAs', { name: finalName }))
        }
      } catch (e: any) {
        MessagePlugin.error(extractApiMessage(e, t('abi.adhoc.file.saveFailed')))
      }
    },
  })
}

/** Rename an ad-hoc file via dialog. */
function renameFile(file: AdhocFile) {
  // Use a ref so the rendered input value stays in sync with the latest
  // user input even if TDesign re-runs the body function mid-edit.
  const newName = ref(file.displayName)
  const dialog = DialogPlugin({
    header: t('abi.adhoc.file.renameTitle'),
    // Render a real input VNode; an HTML string would be escaped and shown as text.
    body: () =>
      h('div', { class: 't-input__wrap' }, [
        h('input', {
          class: 't-input__inner',
          value: newName.value,
          placeholder: t('abi.adhoc.file.newNamePlaceholder'),
          autofocus: true,
          onInput: (e: Event) => {
            newName.value = (e.target as HTMLInputElement).value
          },
          onFocus: (e: Event) => {
            ;(e.target as HTMLInputElement).select()
          },
        }),
      ]),
    confirmBtn: t('common.confirm'),
    cancelBtn: t('common.cancel'),
    onConfirm: async () => {
      const value = newName.value.trim()
      if (!value || value === file.displayName) {
        dialog.hide()
        return
      }
      dialog.hide()
      try {
        const newPath = `ad-hocs/${value}.sql`
        await adhoc.renameAdhocFile(file.path, newPath)
        await adhoc.listAdhocFiles()
      } catch (e: any) {
        MessagePlugin.error(extractApiMessage(e, 'Rename failed'))
      }
    },
  })
}

/** Delete an ad-hoc file with confirmation. */
function deleteFile(file: AdhocFile) {
  const dialog = DialogPlugin.confirm({
    header: t('abi.adhoc.file.deleteConfirm', { name: file.displayName }),
    theme: 'warning',
    confirmBtn: t('common.confirm'),
    cancelBtn: t('common.cancel'),
    onConfirm: async () => {
      dialog.hide()
      try {
        await adhoc.deleteAdhocFile(file.path)
        await adhoc.listAdhocFiles()
        MessagePlugin.success(t('common.deleted'))
      } catch (e: any) {
        MessagePlugin.error(extractApiMessage(e, 'Delete failed'))
      }
    },
  })
}
</script>

<template>
  <div class="abi-view">
    <!-- Left navigation rail -->
    <nav class="abi-nav" :style="{ width: navWidth + 'px' }">
      <div class="abi-nav-title">A-BI</div>
      <!-- Other nav items: flat buttons -->
      <button
          v-for="item in otherNavItems"
          :key="item.key"
          class="abi-nav-item"
          :class="{ active: activeSection === item.key }"
          @click="selectSection(item.key)"
      >
        <t-icon :name="item.icon" size="18px" />
        <span>{{ item.label }}</span>
      </button>
      <!-- Ad-hoc: expandable section with file list -->
      <div class="abi-nav-section">
        <button
          class="abi-nav-item"
          :class="{ active: activeSection === 'adhoc' }"
          @click="toggleAdhoc"
        >
          <t-icon name="data-search" size="18px" />
          <span>{{ t('abi.nav.data') }}</span>
          <t-icon
            :name="adhocExpanded ? 'chevron-down' : 'chevron-right'"
            size="14px"
            class="expand-icon"
          />
        </button>

        <!-- File list (shown when expanded) -->
        <div v-if="adhocExpanded" class="abi-nav-files">
          <div class="files-header">
            <span class="files-label">Queries</span>
            <button
              class="files-add"
              :title="t('abi.adhoc.file.newQuery')"
              @click="createQuery"
            >
              <t-icon name="add" size="16px" />
            </button>
          </div>

          <div v-if="adhoc.filesLoading" class="files-status">
            <t-icon name="loading" size="12px" class="spinning" />
          </div>

          <div
            v-else-if="adhoc.adhocFiles.length === 0"
            class="files-empty"
          >
            {{ t('abi.adhoc.file.empty') }}
          </div>

          <button
            v-for="file in adhoc.adhocFiles"
            :key="file.path"
            class="file-item"
            @click="openFile(file.path)"
          >
            <t-icon name="file" size="14px" class="file-icon" />
            <span class="file-name">{{ file.displayName }}</span>
            <span class="file-actions">
              <!--
                t-icon does not emit a `click` event in Vue 3 (it accepts onClick
                as a prop only), so we must wrap it in a native element and put
                the click handler there. @click.stop prevents the file-open
                handler on the outer button from firing.
              -->
              <span
                class="file-action-icon"
                :title="t('abi.adhoc.file.rename')"
                role="button"
                tabindex="0"
                @click.stop="renameFile(file)"
                @keydown.enter.stop="renameFile(file)"
                @keydown.space.prevent.stop="renameFile(file)"
              >
                <t-icon name="edit" size="12px" />
              </span>
              <span
                class="file-action-icon"
                :title="t('abi.adhoc.file.delete')"
                role="button"
                tabindex="0"
                @click.stop="deleteFile(file)"
                @keydown.enter.stop="deleteFile(file)"
                @keydown.space.prevent.stop="deleteFile(file)"
              >
                <t-icon name="delete" size="12px" />
              </span>
            </span>
          </button>
        </div>
      </div>


    </nav>

    <!-- Resize handle between the left nav and the main content -->
    <SplitHandle orientation="vertical" :label="t('abi.nav.resize')" @resize="onNavResize" />

    <!-- Section content -->
    <section class="abi-content">
      <AbiAgentChat v-if="activeSection === 'agent'" />
      <AbiAdhoc v-else-if="activeSection === 'adhoc'" ref="adhocRef" />
      <AbiDashboard v-else-if="activeSection === 'dashboard'" />
      <AbiKnowledge v-else-if="activeSection === 'knowledge'" />

    </section>
  </div>
</template>

<style scoped>
.abi-view {
  display: flex;
  height: 100%;
  background: var(--bg-primary);
}

/* Left navigation rail (width is set inline by the resize logic) */
.abi-nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
  padding: 16px 12px;
  border-right: 1px solid var(--border);
  background: var(--bg-secondary);
  overflow-y: auto;
}

.abi-nav-title {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: var(--text-muted);
  padding: 4px 12px 12px;
}

.abi-nav-section {
  display: flex;
  flex-direction: column;
}

.abi-nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  border: none;
  border-radius: 8px;
  background: none;
  color: var(--text-secondary);
  font-size: 14px;
  cursor: pointer;
  text-align: left;
  transition: background var(--transition-base), color var(--transition-base);
  flex-shrink: 0;
}

.abi-nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.abi-nav-item.active {
  background: var(--bg-hover);
  color: var(--accent);
  font-weight: 600;
}

.expand-icon {
  margin-left: auto;
  color: var(--text-muted);
  flex-shrink: 0;
}

/* Ad-hoc file list under the nav item */
.abi-nav-files {
  display: flex;
  flex-direction: column;
  padding: 4px 0 4px 18px;
  border-left: 1px solid var(--border);
  margin-left: 18px;
  gap: 2px;
}

.files-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 4px 4px 0;
}

.files-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.files-add {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 4px;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
}

.files-add:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.files-status,
.files-empty {
  font-size: 12px;
  color: var(--text-muted);
  padding: 8px 4px;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 5px 6px;
  border: none;
  border-radius: 5px;
  background: none;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  transition: background var(--transition-base);
}

.file-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.file-icon {
  color: var(--text-muted);
  flex-shrink: 0;
}

.file-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-actions {
  display: none;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}

.file-item:hover .file-actions {
  display: flex;
}

.file-action-icon {
  padding: 2px;
  border-radius: 3px;
  color: var(--text-muted);
}

.file-action-icon:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* Section content */
.abi-content {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
</style>
