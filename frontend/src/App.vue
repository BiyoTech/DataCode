<template>
  <!-- Authentication is enforced by the router guard. -->
  <RouterView />
  <!-- Global hand-over indicator: shown while the API client waits out a
       working-copy relocation (drain), so the user sees "moving" not "failed". -->
  <WorkspaceRelocatingBanner />
</template>

<script setup lang="ts">
import WorkspaceRelocatingBanner from '@/components/WorkspaceRelocatingBanner.vue'
</script>
