<template>
  <!--
    Global relocation banner (design 03 §4.3). Shown while any request is
    waiting out a WORKSPACE_RELOCATING hand-over: the platform is moving the
    working copy to another runtime replica, and the API client replays the
    request automatically. One calm line here replaces every panel failing
    and flashing its own error for the same underlying condition.
  -->
  <Transition name="relocating">
    <div v-if="relocationPending > 0" class="relocating-banner" role="status">
      {{ t('errors.relocating') }}
    </div>
  </Transition>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { relocationPending } from '@/utils/relocating'

const { t } = useI18n()
</script>

<style scoped>
.relocating-banner {
  position: fixed;
  top: 0;
  left: 50%;
  transform: translateX(-50%);
  z-index: 3000;
  padding: 6px 18px;
  border-radius: 0 0 6px 6px;
  background: var(--td-warning-color-1, #fff3e0);
  color: var(--td-warning-color-6, #e37318);
  font-size: 13px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
  pointer-events: none;
}

/* Enter/leave are soft on purpose: the banner appears mid-hand-over and must
   not itself read as an error flashing in. */
.relocating-enter-active,
.relocating-leave-active {
  transition: opacity 0.25s ease;
}
.relocating-enter-from,
.relocating-leave-to {
  opacity: 0;
}
</style>
