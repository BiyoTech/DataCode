<script setup lang="ts">
/**
 * ExecutionStylePicker — autonomous vs interactive execution style selector.
 */
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ExecutionStyle } from '@/types/agent'

const props = defineProps<{
  modelValue: ExecutionStyle
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: ExecutionStyle): void
}>()

const { t } = useI18n()
const showMenu = ref(false)

const executionStyles: { id: ExecutionStyle; icon: string }[] = [
  { id: 'agent', icon: 'play-circle' },
  { id: 'interactive', icon: 'usergroup' },
]

const currentLabel = computed(() => t(`chat.executionStyles.${props.modelValue}`))
const currentIcon = computed(
  () => executionStyles.find((s) => s.id === props.modelValue)?.icon || 'play-circle',
)

function closeMenu(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.closest('.execution-style-picker')) {
    showMenu.value = false
  }
}

onMounted(() => document.addEventListener('click', closeMenu))
onBeforeUnmount(() => document.removeEventListener('click', closeMenu))

function selectStyle(style: ExecutionStyle) {
  emit('update:modelValue', style)
  showMenu.value = false
}
</script>

<template>
  <div class="mode-picker execution-style-picker" @click.stop>
    <button class="mode-btn" :title="t('chat.executionStyle.tooltip')" @click="showMenu = !showMenu">
      <t-icon :name="currentIcon" size="14px" class="mode-icon" />
      <span class="mode-label">{{ currentLabel }}</span>
      <t-icon name="chevron-down" size="10px" class="mode-chevron" />
    </button>
    <div v-if="showMenu" class="mode-menu">
      <div
        v-for="s in executionStyles"
        :key="s.id"
        class="mode-menu-item style-menu-item"
        :class="{ active: modelValue === s.id }"
        @click="selectStyle(s.id)"
      >
        <t-icon :name="s.icon" size="14px" class="mode-menu-icon" />
        <div class="style-menu-text">
          <span class="mode-menu-label">{{ t(`chat.executionStyles.${s.id}`) }}</span>
          <span class="style-menu-desc">{{ t(`chat.executionStyleDesc.${s.id}`) }}</span>
        </div>
        <t-icon v-if="modelValue === s.id" name="check" size="13px" class="mode-check" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.mode-picker {
  position: relative;
}

.mode-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  background: var(--td-bg-color-container);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-small);
  padding: 4px 8px;
  cursor: pointer;
  color: var(--td-text-color-primary);
  font-size: var(--td-font-size-body-small);
  font-weight: 500;
}

.mode-menu {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  min-width: 220px;
  background: var(--td-bg-color-container);
  border: 1px solid var(--td-component-stroke);
  border-radius: var(--td-radius-medium);
  padding: 4px;
  box-shadow: var(--td-shadow-3);
  z-index: 100;
}

.mode-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: var(--td-radius-small);
  cursor: pointer;
  font-size: var(--td-font-size-body-medium);
}

.mode-menu-item:hover {
  background: var(--td-bg-color-container-hover);
}

.mode-menu-item.active {
  color: var(--td-brand-color);
}

.mode-menu-label {
  flex: 1;
}

.mode-check {
  color: var(--td-brand-color);
}

.style-menu-item {
  align-items: flex-start;
}

.style-menu-item .mode-menu-icon {
  margin-top: 2px;
}

.style-menu-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
}

.style-menu-desc {
  font-size: var(--td-font-size-body-small);
  color: var(--td-text-color-placeholder);
  line-height: 1.35;
}
</style>
