<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '@/stores/auth'
import { useHorizontalResize, useVerticalResize } from '@/composables/useResize'
import { useWorkspaceLifecycle } from '@/composables/useWorkspaceLifecycle'
import SidebarPanel from '@/components/layout/SidebarPanel.vue'
import ChatPanel from '@/components/layout/ChatPanel.vue'
import BottomPanel from '@/components/layout/BottomPanel.vue'
import WorkspaceSelector from '@/components/layout/WorkspaceSelector.vue'
import Toolbar from '@/components/layout/Toolbar.vue'
import SplitHandle from '@/components/common/SplitHandle.vue'
import IconButton from '@/components/common/IconButton.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
useWorkspaceLifecycle()

const isFullPage = computed(() => route.meta.fullPage === true)
const isNavOnly = computed(() => route.meta.navOnly === true)
const hideChat = computed(() => route.meta.hideChat === true)

const showSidebar = ref(true)
const showChat = ref(true)
const showBottom = ref(false)

const effectiveShowChat = computed(() => showChat.value && !hideChat.value)

const sidebarResize = useHorizontalResize(240, 180, 400)
const chatResize = useHorizontalResize(380, 200, () => window.innerWidth - 40, true)
const bottomResize = useVerticalResize(240, 120, 500, true)

const sidebarWidth = sidebarResize.size
const chatWidth = chatResize.size
const bottomHeight = bottomResize.size

function onSidebarResize(delta: number) {
  sidebarResize.resizeByDelta(delta)
}

function onChatResize(delta: number) {
  // ``chatResize`` already uses ``reverse: true`` (chat panel is on the right),
  // so the sign is handled inside ``resizeByDelta``. Do NOT negate delta here
  // — that double-inverts and makes drag direction opposite to panel movement.
  chatResize.resizeByDelta(delta)
}

function onBottomResize(delta: number) {
  // ``bottomResize`` already uses ``reverse: true`` (bottom panel is at the
  // bottom), so the sign is handled inside ``resizeByDelta``.
  bottomResize.resizeByDelta(delta)
}

const showUserMenu = ref(false)
const activeSidebarTab = ref<'files' | 'models' | 'diff'>('files')
const activeBottomTab = ref<'git' | 'log'>('git')

function toggleBottomPanel(tab: 'git' | 'log') {
  if (showBottom.value && activeBottomTab.value === tab) {
    showBottom.value = false
  } else {
    activeBottomTab.value = tab
    showBottom.value = true
  }
}

function openSettings() {
  showUserMenu.value = false
  router.push({ path: '/settings', query: { section: 'appearance' } })
}

function openGitSettings() {
  showBottom.value = false
  router.push({ path: '/settings', query: { section: 'git-credentials' } })
}

function handleLogout() {
  showUserMenu.value = false
  authStore.logout()
}

// Close bottom panel when navigating to settings.
watch(
  () => route.name,
  (name) => {
    if (name === 'settings') {
      showBottom.value = false
    }
  },
)
</script>

<template>
  <div class="app-layout">
    <Toolbar
      :show-sidebar="showSidebar"
      :show-chat="showChat"
      :show-chat-toggle="!isFullPage"
      @toggle-sidebar="showSidebar = !showSidebar"
      @toggle-chat="showChat = !showChat"
    >
      <template #center>
        <WorkspaceSelector />
      </template>

      <template #right-extra>
        <div class="user-menu-wrapper">
          <IconButton
            class="toolbar-btn user-btn"
            :label="t('common.userMenu')"
            @click="showUserMenu = !showUserMenu"
          >
            <t-icon name="user-circle" size="18px" />
          </IconButton>
          <div v-if="showUserMenu" class="user-dropdown">
            <div class="user-dropdown-header">
              <span class="user-dropdown-name">admin</span>
              <span class="user-dropdown-email">admin@agenticdata.local</span>
            </div>
            <div class="user-dropdown-divider" />
            <button class="user-dropdown-item" @click="openSettings">
              <t-icon name="setting" size="16px" />
              <span>{{ t('common.settings') }}</span>
            </button>
            <button class="user-dropdown-item danger" @click="handleLogout">
              <t-icon name="poweroff" size="16px" />
              <span>{{ t('login.logout') }}</span>
            </button>
          </div>
          <div v-if="showUserMenu" class="user-menu-backdrop" @click="showUserMenu = false" />
        </div>
      </template>
    </Toolbar>

    <div v-if="isFullPage" class="body-area">
      <RouterView />
    </div>

    <div v-else class="body-area">
      <div class="horizontal-split">
        <aside
          v-show="showSidebar"
          class="panel panel-sidebar"
          :class="{ 'nav-only': isNavOnly }"
          :style="{ width: (isNavOnly ? 40 : sidebarWidth) + 'px' }"
        >
          <SidebarPanel
            :active-tab="activeSidebarTab"
            :nav-only="isNavOnly"
            @change-tab="activeSidebarTab = $event"
            @toggle-bottom="toggleBottomPanel"
          />
        </aside>

        <SplitHandle
          v-show="showSidebar && !isNavOnly"
          orientation="vertical"
          :label="t('layout.resizeSidebar')"
          :value-now="sidebarWidth"
          :class="{ 'resizing-v': sidebarResize.isResizing }"
          @resize="onSidebarResize"
        />

        <div class="main-right-area">
          <div class="upper-area">
            <main class="panel panel-editor">
              <RouterView />
            </main>

            <SplitHandle
              v-show="effectiveShowChat"
              orientation="vertical"
              :label="t('layout.resizeChat')"
              :value-now="chatWidth"
              :class="{ 'resizing-v': chatResize.isResizing }"
              @resize="onChatResize"
            />

            <aside
              v-show="effectiveShowChat"
              class="panel panel-chat"
              :style="{ width: chatWidth + 'px' }"
            >
              <ChatPanel />
            </aside>
          </div>

          <SplitHandle
            v-show="showBottom"
            orientation="horizontal"
            :label="t('layout.resizeBottom')"
            :value-now="bottomHeight"
            :class="{ 'resizing-h': bottomResize.isResizing }"
            @resize="onBottomResize"
          />

          <div
            v-show="showBottom"
            class="panel panel-bottom"
            :style="{ height: bottomHeight + 'px' }"
          >
            <BottomPanel
              :active-tab="activeBottomTab"
              @change-tab="activeBottomTab = $event"
              @close="showBottom = false"
              @go-to-settings="openGitSettings"
            />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
}

.body-area {
  display: flex;
  flex-direction: column;
  flex: 1;
  overflow: hidden;
}

.horizontal-split {
  display: flex;
  flex: 1;
  overflow: hidden;
}

.main-right-area {
  display: flex;
  flex-direction: column;
  flex: 1;
  overflow: hidden;
}

.upper-area {
  display: flex;
  flex: 1;
  overflow: hidden;
}

.panel {
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.panel-sidebar {
  background: var(--bg-secondary);
  border-right: 1px solid var(--border);
  min-width: 180px;
  max-width: 400px;
  flex-shrink: 0;
}

.panel-sidebar.nav-only {
  min-width: 40px;
  max-width: 40px;
}

.panel-editor {
  flex: 1;
  background: var(--bg-primary);
  min-width: 20px;
}

.panel-chat {
  background: var(--bg-secondary);
  border-left: 1px solid var(--border);
  min-width: 200px;
  flex-shrink: 0;
}

.panel-bottom {
  background: var(--bg-secondary);
  border-top: 1px solid var(--border);
  min-height: 120px;
  max-height: 50vh;
  flex-shrink: 0;
}

.resize-handle {
  transition: background var(--transition-base);
  flex-shrink: 0;
  position: relative;
  z-index: 10;
}

.resize-handle-v {
  width: 1px;
  background: var(--border);
  cursor: col-resize;
}

.resize-handle-v:hover,
.resize-handle.resizing-v {
  background: var(--accent);
  width: 3px;
}

.resize-handle-h {
  height: 1px;
  background: var(--border);
  cursor: row-resize;
}

.resize-handle-h:hover,
.resize-handle.resizing-h {
  background: var(--accent);
  height: 3px;
}

.user-menu-wrapper {
  position: relative;
}

.user-menu-backdrop {
  position: fixed;
  inset: 0;
  z-index: 99;
}

.user-dropdown {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  width: 220px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-float-lg);
  z-index: 100;
  overflow: hidden;
}

.user-dropdown-header {
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.user-dropdown-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.user-dropdown-email {
  font-size: 11px;
  color: var(--text-muted);
}

.user-dropdown-divider {
  height: 1px;
  background: var(--border);
}

.user-dropdown-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 14px;
  background: none;
  border: none;
  color: var(--text-primary);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}

.user-dropdown-item:hover {
  background: var(--bg-hover);
}

.user-dropdown-item.danger {
  color: var(--error);
}

.user-dropdown-item.danger:hover {
  background: var(--error-subtle);
}
</style>
