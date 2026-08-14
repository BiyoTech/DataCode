<script setup lang="ts">
/**
 * Identity picker.
 *
 * Renders the tenant identities available to an account, grouped into primary
 * accounts (tenants the user created) and sub accounts (tenants the user was
 * invited to). Reused by the login flow and the in-app account switcher.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Building1Icon, CheckIcon, UserIcon } from 'tdesign-icons-vue-next'

import type { MembershipInfo } from '@/utils/authSession'

const props = defineProps<{
  memberships: MembershipInfo[]
  /** Tenant currently active, highlighted as "current" when switching. */
  activeTenantId?: string
  /** Tenant awaiting a server round-trip. */
  pendingTenantId?: string
  disabled?: boolean
}>()

const emit = defineEmits<{ (e: 'select', tenantId: string): void }>()

const { t } = useI18n()

const primaryAccounts = computed(() => props.memberships.filter((m) => m.isOwner))
const subAccounts = computed(() => props.memberships.filter((m) => !m.isOwner))

const roleLabels: Record<string, string> = {
  admin: 'account.roleAdmin',
  developer: 'account.roleDeveloper',
  viewer: 'account.roleViewer',
}

function roleLabel(role: string): string {
  return t(roleLabels[role] ?? 'account.roleViewer')
}

function handleSelect(membership: MembershipInfo) {
  if (props.disabled || props.pendingTenantId) return
  emit('select', membership.tenantId)
}
</script>

<template>
  <div class="identity-select">
    <section v-if="primaryAccounts.length" class="identity-group">
      <h3 class="group-title">{{ t('identity.primaryAccounts') }}</h3>
      <button
        v-for="membership in primaryAccounts"
        :key="membership.tenantId"
        type="button"
        class="identity-card"
        :class="{
          'is-active': membership.tenantId === activeTenantId,
          'is-pending': membership.tenantId === pendingTenantId,
        }"
        :disabled="disabled"
        @click="handleSelect(membership)"
      >
        <span class="card-icon"><Building1Icon size="18px" /></span>
        <span class="card-body">
          <span class="card-head">
            <span class="card-title">{{ membership.tenantName }}</span>
            <span v-if="membership.isLastUsed" class="badge badge-accent">
              {{ t('identity.lastUsed') }}
            </span>
            <span v-if="membership.tenantId === activeTenantId" class="badge badge-current">
              {{ t('identity.current') }}
            </span>
          </span>
          <span class="card-meta">
            {{ t('identity.ownerLabel') }} · {{ roleLabel(membership.role) }}
          </span>
        </span>
        <span v-if="membership.tenantId === activeTenantId" class="card-check">
          <CheckIcon size="16px" />
        </span>
      </button>
    </section>

    <section v-if="subAccounts.length" class="identity-group">
      <h3 class="group-title">{{ t('identity.subAccounts') }}</h3>
      <button
        v-for="membership in subAccounts"
        :key="membership.tenantId"
        type="button"
        class="identity-card"
        :class="{
          'is-active': membership.tenantId === activeTenantId,
          'is-pending': membership.tenantId === pendingTenantId,
        }"
        :disabled="disabled"
        @click="handleSelect(membership)"
      >
        <span class="card-icon"><UserIcon size="18px" /></span>
        <span class="card-body">
          <span class="card-head">
            <span class="card-title">{{ membership.tenantName }}</span>
            <span v-if="membership.isLastUsed" class="badge badge-accent">
              {{ t('identity.lastUsed') }}
            </span>
            <span v-if="membership.tenantId === activeTenantId" class="badge badge-current">
              {{ t('identity.current') }}
            </span>
          </span>
          <span class="card-meta">
            <template v-if="membership.ownerUsername">
              {{ t('identity.ownerLabel') }}: {{ membership.ownerUsername }} ·
            </template>
            {{ roleLabel(membership.role) }}
          </span>
        </span>
        <span v-if="membership.tenantId === activeTenantId" class="card-check">
          <CheckIcon size="16px" />
        </span>
      </button>
    </section>

    <p v-if="!memberships.length" class="identity-empty">{{ t('identity.empty') }}</p>
  </div>
</template>

<style scoped>
.identity-select {
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-height: 420px;
  overflow-y: auto;
  padding-right: 2px;
}

.identity-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.group-title {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
  margin: 0 0 2px;
}

.identity-card {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 12px 14px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  cursor: pointer;
  text-align: left;
  color: var(--text-primary);
  transition:
    transform 0.16s ease,
    border-color 0.16s ease,
    background 0.16s ease,
    box-shadow 0.16s ease;
}

.identity-card:hover:not(:disabled) {
  transform: translateY(-1px);
  border-color: var(--accent);
  background: var(--accent-subtler);
  box-shadow: var(--shadow-float);
}

.identity-card:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.identity-card.is-active {
  border-color: var(--accent);
  background: var(--accent-subtler);
}

.identity-card.is-pending {
  cursor: progress;
  opacity: 0.7;
}

.card-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  border-radius: 9px;
  background: var(--accent-subtler);
  color: var(--accent);
}

.card-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-meta {
  font-size: 12px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.badge {
  font-size: 11px;
  font-weight: 500;
  padding: 1px 7px;
  border-radius: 5px;
  white-space: nowrap;
}

.badge-accent {
  background: var(--accent-subtler);
  color: var(--accent);
}

.badge-current {
  background: var(--success-subtle);
  color: var(--success);
}

.card-check {
  color: var(--accent);
  display: flex;
  flex-shrink: 0;
}

.identity-empty {
  font-size: 13px;
  color: var(--text-muted);
  text-align: center;
  padding: 20px 0;
}
</style>
