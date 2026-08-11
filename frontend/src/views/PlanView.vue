<script setup lang="ts">
/**
 * PlanView - Full-page view for Plan & Apply workflow.
 * Renders the PlanPanel component within the main content area.
 */
import PlanPanel from '@/components/plan/PlanPanel.vue'
</script>

<template>
  <div class="plan-view">
    <PlanPanel />
  </div>
</template>

<style scoped>
.plan-view {
  width: 100%;
  height: 100%;
  overflow: hidden;
}
</style>
