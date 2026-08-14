<script setup lang="ts">
/**
 * SettingsView - Databricks-style settings page.
 * Left sidebar navigation + right content area.
 */
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { setLocale } from '@/i18n'
import { useThemeStore } from '@/stores/theme'
import type { ThemeMode } from '@/stores/theme'
import {
  getUserTimezone,
  getAutoDetectedTimezone,
  getTimezoneMode,
  setUserTimezone,
  resetToAutoTimezone,
  COMMON_TIMEZONES,
} from '@/utils/timezone'
import GitCredentialsList from '@/components/settings/GitCredentialsList.vue'
import AccountSecurity from '@/components/settings/AccountSecurity.vue'
import MemberManagement from '@/components/settings/MemberManagement.vue'
import RoleManagement from '@/components/settings/RoleManagement.vue'
import { usePermission } from '@/composables/usePermission'
import { useAuthStore } from '@/stores/auth'
import { useRbacStore } from '@/stores/rbac'

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const themeStore = useThemeStore()
const authStore = useAuthStore()
const rbacStore = useRbacStore()
const { can } = usePermission()

const initialSection = computed(() => {
  const section = route.query.section
  return typeof section === 'string' ? section : 'appearance'
})

// Navigation structure (labels use i18n keys)
interface NavSection {
  id: string
  icon: string
  titleKey: string
  items: { id: string; labelKey: string }[]
}

/**
 * Navigation is derived from the caller's permissions, which the gateway
 * resolves from the RBAC tables (the API enforces them as well).
 */
const navSections = computed<NavSection[]>(() => [
  {
    id: 'workspace',
    icon: '⚙',
    titleKey: 'settings.navWorkspace',
    items: [
      { id: 'appearance', labelKey: 'settings.appearance' },
      { id: 'connections', labelKey: 'settings.connections' },
      { id: 'advanced', labelKey: 'settings.advanced' },
    ],
  },
  {
    id: 'organization',
    icon: '🏢',
    titleKey: 'settings.navOrganization',
    items: [
      ...(can('tenant:member:read') ? [{ id: 'member-management', labelKey: 'members.title' }] : []),
      ...(can('tenant:role:read') ? [{ id: 'role-management', labelKey: 'rbac.title' }] : []),
    ],
  },
  {
    id: 'user',
    icon: '👤',
    titleKey: 'settings.navUser',
    items: [
      { id: 'profile', labelKey: 'settings.profile' },
      { id: 'account-security', labelKey: 'account.title' },
      { id: 'preferences', labelKey: 'settings.preferences' },
      { id: 'git-credentials', labelKey: 'settings.gitCredentials' },
      { id: 'notifications', labelKey: 'settings.notifications' },
    ],
  },
])

const activeItem = ref(initialSection.value)

watch(initialSection, (section) => {
  activeItem.value = section
})

/** Localized role of the active tenant identity. */
/**
 * Label of the active tenant role. Built-in codes are localized; a custom role
 * is shown with the display name the organization gave it (resolved by /me).
 */
const currentRoleLabel = computed(() => {
  const labels: Record<string, string> = {
    admin: 'account.roleAdmin',
    developer: 'account.roleDeveloper',
    viewer: 'account.roleViewer',
  }
  const role = authStore.user?.role
  if (!role) return ''
  const key = labels[role]
  return key ? t(key) : rbacStore.tenantRoleName || role
})

function goBack() {
  router.push('/chat')
}

// Language settings
const currentLocale = ref(locale.value)
const localeOptions = [
  { value: 'en', label: 'English' },
  { value: 'zh-CN', label: '简体中文' },
]

function changeLocale(newLocale: string) {
  currentLocale.value = newLocale
  setLocale(newLocale)
}

// Timezone settings
const timezoneMode = ref(getTimezoneMode())
const selectedTimezone = ref(getUserTimezone())
const autoDetectedTz = computed(() => getAutoDetectedTimezone())

function changeTimezoneMode(mode: 'auto' | 'manual') {
  timezoneMode.value = mode
  if (mode === 'auto') {
    resetToAutoTimezone()
    selectedTimezone.value = getAutoDetectedTimezone()
  }
}

function changeTimezone(tz: string) {
  selectedTimezone.value = tz
  setUserTimezone(tz, 'manual')
}

// Theme settings
const themeOptions: { value: ThemeMode; labelKey: string }[] = [
  { value: 'system', labelKey: 'settings.themeSystem' },
  { value: 'dark', labelKey: 'settings.themeDark' },
  { value: 'light', labelKey: 'settings.themeLight' },
]

function changeTheme(mode: ThemeMode) {
  themeStore.setMode(mode)
}
</script>

<template>
  <div class="settings-layout">
    <!-- Left: Navigation sidebar -->
    <nav class="settings-nav">
      <div class="nav-header">
        <button class="back-btn" @click="goBack">← Back</button>
        <h2 class="nav-title">{{ t('common.settings') }}</h2>
      </div>

      <div class="nav-sections">
        <div
          v-for="section in navSections"
          v-show="section.items.length"
          :key="section.id"
          class="nav-section"
        >
          <div class="nav-section-title">
            <span class="nav-section-icon">{{ section.icon }}</span>
            <span>{{ t(section.titleKey) }}</span>
          </div>
          <button
            v-for="item in section.items"
            :key="item.id"
            :class="['nav-item', { active: activeItem === item.id }]"
            @click="activeItem = item.id"
          >
            {{ t(item.labelKey) }}
          </button>
        </div>
      </div>
    </nav>

    <!-- Right: Content area -->
    <main class="settings-content">
      <!-- Appearance (Workspace-level: timezone) -->
      <div v-if="activeItem === 'appearance'" class="content-page">
        <h3 class="page-title">{{ t('settings.appearance') }}</h3>
        <p class="page-desc">{{ t('settings.appearanceDesc') }}</p>

        <div class="settings-card">
          <!-- Theme selector -->
          <div class="setting-row">
            <div class="setting-info">
              <span class="setting-label">{{ t('settings.themeLabel') }}</span>
              <span class="setting-hint">{{ t('settings.themeHint') }}</span>
            </div>
            <div class="segmented-control">
              <button
                v-for="option in themeOptions"
                :key="option.value"
                :class="['segmented-btn', { active: themeStore.mode === option.value }]"
                @click="changeTheme(option.value)"
              >
                {{ t(option.labelKey) }}
              </button>
            </div>
          </div>

          <div class="setting-divider" />

          <div class="setting-row">
            <div class="setting-info">
              <span class="setting-label">{{ t('settings.timezoneLabel') }}</span>
              <span class="setting-hint">{{ t('settings.timezoneHint') }}</span>
            </div>
            <div class="setting-control">
              <div class="radio-group">
                <label class="radio-label">
                  <input
                    type="radio"
                    value="auto"
                    :checked="timezoneMode === 'auto'"
                    @change="changeTimezoneMode('auto')"
                  />
                  <span>{{ t('settings.timezoneAuto', { tz: autoDetectedTz }) }}</span>
                </label>
                <label class="radio-label">
                  <input
                    type="radio"
                    value="manual"
                    :checked="timezoneMode === 'manual'"
                    @change="changeTimezoneMode('manual')"
                  />
                  <span>{{ t('settings.timezoneManual') }}</span>
                </label>
              </div>
              <select
                v-if="timezoneMode === 'manual'"
                class="setting-select"
                :value="selectedTimezone"
                @change="changeTimezone(($event.target as any).value)"
              >
                <option v-for="tz in COMMON_TIMEZONES" :key="tz" :value="tz">{{ tz }}</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      <!-- Preferences (User-level: language) -->
      <div v-else-if="activeItem === 'preferences'" class="content-page">
        <h3 class="page-title">{{ t('settings.preferences') }}</h3>
        <p class="page-desc">{{ t('settings.preferencesDesc') }}</p>

        <div class="settings-card">
          <div class="setting-row">
            <div class="setting-info">
              <span class="setting-label">{{ t('settings.languageLabel') }}</span>
              <span class="setting-hint">{{ t('settings.languageHint') }}</span>
            </div>
            <select
              class="setting-select"
              :value="currentLocale"
              @change="changeLocale(($event.target as any).value)"
            >
              <option v-for="opt in localeOptions" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </option>
            </select>
          </div>
        </div>
      </div>

      <!-- Profile -->
      <div v-else-if="activeItem === 'profile'" class="content-page">
        <h3 class="page-title">{{ t('settings.profile') }}</h3>
        <p class="page-desc">{{ t('settings.profileDesc') }}</p>

        <div class="settings-card">
          <div class="setting-row">
            <div class="setting-info">
              <span class="setting-label">{{ t('login.username') }}</span>
              <span class="setting-hint">{{ t('settings.usernameHint') }}</span>
            </div>
            <span class="setting-value">{{ authStore.user?.username }}</span>
          </div>

          <div class="setting-divider" />

          <div class="setting-row">
            <div class="setting-info">
              <span class="setting-label">{{ t('account.organization') }}</span>
              <span class="setting-hint">{{ t('settings.organizationHint') }}</span>
            </div>
            <span class="setting-value">{{ authStore.user?.tenantName }}</span>
          </div>

          <div class="setting-divider" />

          <div class="setting-row">
            <div class="setting-info">
              <span class="setting-label">{{ t('account.role') }}</span>
              <span class="setting-hint">{{ t('settings.roleHint') }}</span>
            </div>
            <span class="role-badge">{{ currentRoleLabel }}</span>
          </div>
        </div>
      </div>

      <!-- Account security (password change) -->
      <div v-else-if="activeItem === 'account-security'" class="content-page">
        <h3 class="page-title">{{ t('account.title') }}</h3>
        <p class="page-desc">{{ t('account.description') }}</p>

        <AccountSecurity />
      </div>

      <!-- Tenant member management -->
      <div v-else-if="activeItem === 'member-management'" class="content-page">
        <h3 class="page-title">{{ t('members.title') }}</h3>
        <p class="page-desc">{{ t('members.description') }}</p>

        <MemberManagement />
      </div>

      <!-- Role and permission management -->
      <div v-else-if="activeItem === 'role-management'" class="content-page">
        <h3 class="page-title">{{ t('rbac.title') }}</h3>
        <p class="page-desc">{{ t('rbac.description') }}</p>

        <RoleManagement />
      </div>

      <!-- Git Credentials -->
      <div v-else-if="activeItem === 'git-credentials'" class="content-page">
        <h3 class="page-title">{{ t('settings.gitCredentials') }}</h3>
        <p class="page-desc">{{ t('settings.gitCredentialsDesc') }}</p>

        <GitCredentialsList />
      </div>

      <!-- Placeholder for other sections -->
      <div v-else class="content-page">
        <h3 class="page-title">{{ activeItem.replace('-', ' ').replace(/\b\w/g, c => c.toUpperCase()) }}</h3>
        <p class="page-desc">This section is under development.</p>
        <div class="settings-card">
          <div class="empty-placeholder">Coming soon...</div>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
.settings-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}

/* Left navigation */
.settings-nav {
  width: 240px;
  min-width: 200px;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.nav-header {
  padding: 16px 16px 12px;
  border-bottom: 1px solid var(--border);
}

.back-btn {
  background: none;
  border: none;
  color: var(--accent);
  font-size: 12px;
  cursor: pointer;
  padding: 4px 0;
  margin-bottom: 8px;
}

.back-btn:hover {
  text-decoration: underline;
}

.nav-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
}

.nav-sections {
  padding: 12px 0;
}

.nav-section {
  margin-bottom: 8px;
}

.nav-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.nav-section-icon {
  font-size: 14px;
}

.nav-item {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  padding: 7px 16px 7px 38px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
  border-left: 2px solid transparent;
}

.nav-item:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.nav-item.active {
  color: var(--text-primary);
  background: var(--bg-hover);
  border-left-color: var(--accent);
  font-weight: 500;
}

/* Right content */
.settings-content {
  flex: 1;
  overflow-y: auto;
  padding: 32px 40px;
  background: var(--bg-primary);
}

.content-page {
  max-width: 720px;
}

.page-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}

.page-desc {
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 24px;
}

.settings-card {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 4px 0;
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  gap: 24px;
}

.setting-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.setting-label {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
}

.setting-hint {
  font-size: 12px;
  color: var(--text-muted);
}

.setting-value {
  font-size: 14px;
  color: var(--text-primary);
  white-space: nowrap;
}

.setting-select {
  padding: 6px 12px;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 13px;
  min-width: 160px;
}

.setting-control {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-end;
}

.radio-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.radio-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
}

/* Segmented control (theme selector) */
.segmented-control {
  display: flex;
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.segmented-btn {
  padding: 6px 14px;
  background: none;
  border: none;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background-color var(--transition-fast), color var(--transition-fast);
  white-space: nowrap;
}

.segmented-btn:hover {
  color: var(--text-primary);
}

.segmented-btn.active {
  background: var(--accent);
  color: var(--accent-foreground);
  font-weight: 600;
}

.segmented-btn + .segmented-btn {
  border-left: 1px solid var(--border);
}

.setting-divider {
  height: 1px;
  background: var(--border);
  margin: 0 20px;
}

.role-badge {
  background: var(--accent-dim);
  color: var(--accent);
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 500;
}

.btn-outline {
  padding: 6px 14px;
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-primary);
  font-size: 12px;
  cursor: pointer;
}

.btn-outline:hover {
  background: var(--bg-hover);
  border-color: var(--accent);
}

.empty-placeholder {
  padding: 32px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}
</style>
