<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TimelineEntry } from '@/types/agent'

const { t } = useI18n()

const props = defineProps<{
  entry: TimelineEntry
  /**
   * Approval sender injected by the owning panel (e.g. chatStore.sendApproval
   * or chatbiStore.sendApproval). Required so this card stays decoupled from
   * any specific store and works across scenarios.
   */
  onApprove: (requestId: string, approved: boolean, reason?: string) => void | Promise<void>
}>()

const title = computed(() => props.entry.payload.title || 'Action Required')
const description = computed(() => props.entry.payload.description || '')
const toolName = computed(() => props.entry.payload.tool_name || '')
const toolLevel = computed(() => props.entry.payload.tool_level || 2)
const riskBadgeLabel = computed(() => props.entry.payload.risk_badge_label || `L${toolLevel.value}`)
const riskBadgeTone = computed(() => props.entry.payload.risk_badge_tone || 'low')
const requestId = computed(() => props.entry.payload.request_id || '')
const isPending = computed(() => props.entry.status === 'pending')
const isApproved = computed(() => props.entry.status === 'success')
const isRejected = computed(() => props.entry.status === 'cancelled')

// Command-level approval extensions
const commands = computed(() => props.entry.payload.commands as string[] | undefined)
const ruleName = computed(() => props.entry.payload.rule_name as string | undefined)
const reason = computed(() => props.entry.payload.reason as string | undefined)
const isCommandApproval = computed(() => props.entry.payload.request_type === 'command_approval')

const riskBadgeClass = computed(() => {
  return `risk-${riskBadgeTone.value}`
})

function approve() {
  props.onApprove(requestId.value, true)
}

function reject() {
  props.onApprove(requestId.value, false, 'User rejected')
}
</script>

<template>
  <div class="approval-card" :class="{ resolved: !isPending }">
    <div class="approval-header">
      <span class="approval-icon">⚠</span>
      <span class="approval-title">{{ title }}</span>
      <span v-if="toolName" class="tool-name">{{ toolName }}</span>
      <span class="risk-badge" :class="riskBadgeClass">
        {{ riskBadgeLabel }}
      </span>
    </div>

    <div v-if="description" class="approval-description">
      {{ description }}
    </div>

    <!-- Command code block (shell command security policy) -->
    <div v-if="isCommandApproval && commands && commands.length > 0" class="command-list">
      <div class="command-list-header">{{ t('chat.approval.commandList') }}</div>
      <pre class="command-code"><code><template v-for="(cmd, idx) in commands" :key="idx">{{ cmd }}<br v-if="idx < commands.length - 1" /></template></code></pre>
    </div>

    <!-- Rule name + reason (command-level approval) -->
    <div v-if="isCommandApproval && (ruleName || reason)" class="approval-meta">
      <div v-if="ruleName" class="meta-row">
        <span class="meta-label">{{ t('chat.approval.rule') }}</span>
        <span class="meta-value rule-name">{{ ruleName }}</span>
      </div>
      <div v-if="reason" class="meta-row">
        <span class="meta-label">{{ t('chat.approval.reason') }}</span>
        <span class="meta-value">{{ reason }}</span>
      </div>
    </div>

    <div v-if="isPending" class="approval-actions">
      <button class="btn-approve" @click="approve">
        {{ t('chat.approval.execute') }}
      </button>
      <button class="btn-reject" @click="reject">
        {{ t('chat.approval.cancel') }}
      </button>
    </div>
    <div v-else class="approval-resolved">
      <span v-if="isApproved" class="resolved-approved">✓ {{ t('chat.approval.executed') }}</span>
      <span v-if="isRejected" class="resolved-rejected">✗ {{ t('chat.approval.cancelled') }}</span>
    </div>
  </div>
</template>

<style scoped>
.approval-card {
  max-width: 90%;
  border-radius: 10px;
  padding: 14px 18px;
  background: var(--bg-secondary);
  border: 2px solid var(--warning);
  transition: border-color 0.3s ease, opacity 0.3s ease;
}

.approval-card.resolved {
  border-color: var(--border);
  opacity: 0.7;
}

.approval-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.approval-icon {
  font-size: 16px;
}

.approval-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.tool-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  background: var(--accent-subtle);
  padding: 1px 6px;
  border-radius: 4px;
}

.risk-badge {
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
}

.risk-low {
  background: var(--success-subtle);
  color: var(--success);
}

.risk-medium {
  background: var(--warning-subtle);
  color: var(--warning);
}

.risk-high {
  background: var(--error-subtle);
  color: var(--error);
}

.approval-description {
  font-size: 13px;
  color: var(--text-secondary);
  margin-top: 8px;
  line-height: 1.5;
}

/* Command code block */
.command-list {
  margin-top: 12px;
}

.command-list-header {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.command-code {
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 14px;
  font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-primary);
  overflow-x: auto;
  max-height: 200px;
  overflow-y: auto;
  margin: 0;
}

.command-code code {
  white-space: pre;
}

/* Rule / reason metadata */
.approval-meta {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.meta-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}

.meta-label {
  color: var(--text-secondary);
  font-weight: 500;
  flex-shrink: 0;
  min-width: 48px;
}

.meta-value {
  color: var(--text-primary);
  word-break: break-word;
}

.meta-value.rule-name {
  font-weight: 600;
  color: var(--warning);
}

.approval-actions {
  display: flex;
  gap: 10px;
  margin-top: 14px;
}

.btn-approve {
  padding: 8px 20px;
  border-radius: 8px;
  border: none;
  background: var(--success);
  color: var(--accent-foreground);
  font-weight: 600;
  font-size: 13px;
  cursor: pointer;
  transition: transform 0.1s ease, box-shadow 0.2s ease;
}

.btn-approve:hover {
  transform: scale(1.02);
  box-shadow: 0 2px 12px rgba(64, 160, 43, 0.3);
}

.btn-reject {
  padding: 8px 20px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-secondary);
  font-weight: 500;
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.2s ease, color 0.2s ease;
}

.btn-reject:hover {
  border-color: var(--error);
  color: var(--error);
}

.approval-resolved {
  margin-top: 10px;
  font-size: 13px;
}

.resolved-approved {
  color: var(--success);
}

.resolved-rejected {
  color: var(--error);
}
</style>
