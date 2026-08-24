<script setup lang="ts">
/**
 * PlanView - Plan & Apply page container with two top-level tabs.
 *
 * - sqlwarehouse: SQLMesh plan & apply against the current virtual
 *   environment (the existing PlanPanel, unchanged).
 * - asset-dag: the environment's Dagster deployment — code-location status,
 *   manual reload and cross-deployment definition diff. Cross-environment
 *   promotion lands here once the Phase 5 promote endpoint exists.
 *
 * Both panels stay mounted (v-show) so an in-flight plan keeps polling while
 * the user inspects the deployment tab. The active tab is mirrored into the
 * URL query (?tab=asset-dag) so a refresh restores it.
 */
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import PlanPanel from '@/components/plan/PlanPanel.vue'
import AssetDagDeployPanel from '@/components/plan/AssetDagDeployPanel.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

type PageTab = 'sqlwarehouse' | 'assetdag'

const activeTab = ref<PageTab>(route.query.tab === 'asset-dag' ? 'assetdag' : 'sqlwarehouse')

// Keep the tab in the URL (dropped entirely for the default tab) so a refresh
// or a shared link restores it.
watch(activeTab, (tab) => {
  router.replace({ query: { ...route.query, tab: tab === 'assetdag' ? 'asset-dag' : undefined } })
})
</script>

<template>
  <div class="plan-view">
    <!-- Top-level tabs -->
    <div class="page-tabs">
      <button
        class="page-tab"
        :class="{ active: activeTab === 'sqlwarehouse' }"
        @click="activeTab = 'sqlwarehouse'"
      >
        {{ t('plan.tabSqlWarehouse') }}
      </button>
      <button
        class="page-tab"
        :class="{ active: activeTab === 'assetdag' }"
        @click="activeTab = 'assetdag'"
      >
        {{ t('plan.tabAssetDag') }}
      </button>
    </div>

    <!-- Both panels stay alive; only visibility toggles. -->
    <div class="page-content">
      <PlanPanel v-show="activeTab === 'sqlwarehouse'" />
      <AssetDagDeployPanel v-show="activeTab === 'assetdag'" />
    </div>
  </div>
</template>

<style scoped>
.plan-view {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  overflow: hidden;
}

.page-tabs {
  display: flex;
  padding: 0 12px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
}

.page-tab {
  padding: 9px 16px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
}

.page-tab:hover {
  color: var(--text-primary);
}

.page-tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}

.page-content {
  flex: 1;
  overflow: hidden;
}

.page-content > * {
  height: 100%;
}
</style>
