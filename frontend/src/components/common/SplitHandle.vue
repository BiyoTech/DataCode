<script setup lang="ts">
/**
 * SplitHandle — a thin draggable handle used to resize adjacent panels.
 */
import { ref, onBeforeUnmount, computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** "vertical" = drag horizontally (resize width); "horizontal" = drag vertically (resize height). */
    orientation?: 'vertical' | 'horizontal'
    /** Accessible name for screen readers. */
    label?: string
    /** Optional current size for aria-valuenow. */
    valueNow?: number
    /** Keyboard resize step in pixels. */
    step?: number
  }>(),
  {
    orientation: 'vertical',
    label: 'Resize panel',
    step: 10,
  },
)

const emit = defineEmits<{
  /** Cumulative pixel delta during the current drag gesture. */
  (e: 'resize', delta: number): void
}>()

const dragging = ref(false)
let lastPos = 0

const ariaOrientation = computed(() =>
  props.orientation === 'vertical' ? 'vertical' : 'horizontal',
)

function applyDelta(delta: number) {
  if (delta !== 0) {
    emit('resize', delta)
  }
}

function onMouseDown(e: MouseEvent) {
  if (e.button !== 0) return
  e.preventDefault()
  dragging.value = true
  lastPos = props.orientation === 'vertical' ? e.clientX : e.clientY
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
  document.body.style.cursor =
    props.orientation === 'vertical' ? 'col-resize' : 'row-resize'
  document.body.style.userSelect = 'none'
}

function onMouseMove(e: MouseEvent) {
  if (!dragging.value) return
  const cur = props.orientation === 'vertical' ? e.clientX : e.clientY
  const delta = cur - lastPos
  if (delta !== 0) {
    lastPos = cur
    applyDelta(delta)
  }
}

function onMouseUp() {
  dragging.value = false
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
}

function onKeydown(e: KeyboardEvent) {
  const step = e.shiftKey ? props.step * 4 : props.step
  if (props.orientation === 'vertical') {
    if (e.key === 'ArrowLeft') {
      e.preventDefault()
      applyDelta(-step)
    } else if (e.key === 'ArrowRight') {
      e.preventDefault()
      applyDelta(step)
    }
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    applyDelta(-step)
  } else if (e.key === 'ArrowDown') {
    e.preventDefault()
    applyDelta(step)
  }
}

onBeforeUnmount(() => {
  document.removeEventListener('mousemove', onMouseMove)
  document.removeEventListener('mouseup', onMouseUp)
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
})
</script>

<template>
  <div
    class="split-handle"
    :class="[orientation, { dragging }]"
    role="separator"
    tabindex="0"
    :aria-orientation="ariaOrientation"
    :aria-label="label"
    :aria-valuenow="valueNow"
    @mousedown="onMouseDown"
    @keydown="onKeydown"
  />
</template>

<style scoped>
.split-handle {
  position: relative;
  flex-shrink: 0;
  background: transparent;
  z-index: 2;
  outline: none;
}

.split-handle:focus-visible::before {
  background: var(--accent);
}

.split-handle.vertical {
  width: 6px;
  cursor: col-resize;
}

.split-handle.horizontal {
  height: 6px;
  cursor: row-resize;
}

.split-handle::before {
  content: '';
  position: absolute;
  background: var(--border);
  transition: background 0.15s ease;
}

.split-handle.vertical::before {
  top: 0;
  bottom: 0;
  left: 50%;
  width: 1px;
  transform: translateX(-50%);
}

.split-handle.horizontal::before {
  left: 0;
  right: 0;
  top: 50%;
  height: 1px;
  transform: translateY(-50%);
}

.split-handle:hover::before,
.split-handle.dragging::before {
  background: var(--accent);
}
</style>
