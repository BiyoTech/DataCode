<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { useGitStore } from '@/stores/git'
import { useWorkspaceStore } from '@/stores/workspace'
import GitAuthPanel from './GitAuthPanel.vue'
import GitInitPanel from './GitInitPanel.vue'
import GitDetailPanel from './GitDetailPanel.vue'

const gitStore = useGitStore()
const workspaceStore = useWorkspaceStore()

const emit = defineEmits<{
  (e: 'goToSettings'): void
}>()

onMounted(() => {
  gitStore.refreshPanelState()
})

// Re-check when workspace changes
watch(() => workspaceStore.currentWorkspaceId, () => {
  gitStore.refreshPanelState()
})
</script>

<template>
  <div class="git-panel">
    <div v-if="gitStore.isLoading && !gitStore.panelState" class="git-loading">
      Loading...
    </div>

    <!-- State A: Authorization (guide to settings) -->
    <GitAuthPanel v-if="gitStore.panelState === 'auth'" @go-to-settings="emit('goToSettings')" />

    <!-- State B: Initialize -->
    <GitInitPanel v-else-if="gitStore.panelState === 'init'" />

    <!-- State C: Detail (changes/commit/push) -->
    <GitDetailPanel v-else-if="gitStore.panelState === 'detail'" />
  </div>
</template>

<style scoped>
.git-panel {
  display: flex;
  flex-direction: row;
  height: 100%;
  overflow: hidden;
}

.git-loading {
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  width: 100%;
}
</style>
