<script setup lang="ts">
/**
 * IconButton — accessible icon-only button with required aria-label.
 */
defineOptions({ inheritAttrs: false })

withDefaults(
  defineProps<{
    label: string
    title?: string
    disabled?: boolean
    active?: boolean
  }>(),
  {
    disabled: false,
    active: false,
  },
)

defineEmits<{
  click: [event: MouseEvent]
}>()
</script>

<template>
  <button
    type="button"
    class="icon-button"
    :class="[{ active }, $attrs.class]"
    :aria-label="label"
    :title="title ?? label"
    :disabled="disabled"
    @click="$emit('click', $event)"
  >
    <span class="icon-button-content" aria-hidden="true">
      <slot />
    </span>
  </button>
</template>

<style scoped>
.icon-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: none;
  background: transparent;
  cursor: pointer;
}

.icon-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.icon-button-content {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
</style>
