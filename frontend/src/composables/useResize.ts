/**
 * Resize composable for panel dragging functionality
 * Supports horizontal (left/right) and vertical (top/bottom) resizing
 */
import { ref, onMounted, onUnmounted, watch } from 'vue'

export interface ResizeOptions {
  /** Initial size in pixels */
  initialSize: number
  /** Minimum size in pixels */
  minSize: number
  /** Maximum size in pixels, or a getter for a dynamic limit (e.g. viewport-based) */
  maxSize: number | (() => number)
  /** Resize direction: 'horizontal' or 'vertical' */
  direction: 'horizontal' | 'vertical'
  /** Reverse drag direction (for right-side or bottom panels) */
  reverse?: boolean
  /** Callback when resize starts */
  onResizeStart?: () => void
  /** Callback when resize ends */
  onResizeEnd?: () => void
  /** Callback when size changes */
  onSizeChange?: (size: number) => void
}

export interface UseResizeReturn {
  /** Current size in pixels */
  size: ReturnType<typeof ref<number>>
  /** Whether resize is currently active */
  isResizing: ReturnType<typeof ref<boolean>>
  /** Start resize operation */
  startResize: (event: MouseEvent) => void
  /** Apply a pixel delta to the current size (respects min/max). */
  resizeByDelta: (delta: number) => void
  /** Handle keyboard arrow keys for accessible resizing. */
  onKeydown: (event: KeyboardEvent) => void
}

export function useResize(options: ResizeOptions): UseResizeReturn {
  const { 
    initialSize, 
    minSize, 
    maxSize, 
    direction,
    reverse = false,
    onResizeStart, 
    onResizeEnd, 
    onSizeChange 
  } = options

  const size = ref(initialSize)
  const isResizing = ref(false)
  let startPos = 0
  let startSize = 0

  const handleMouseMove = (event: MouseEvent) => {
    if (!isResizing.value) return

    const currentPos = direction === 'horizontal' ? event.clientX : event.clientY
    const delta = currentPos - startPos
    
    // Calculate new size based on direction (reverse for right/bottom panels)
    let newSize = reverse ? startSize - delta : startSize + delta

    // Apply constraints (maxSize may be a dynamic getter)
    const max = typeof maxSize === 'function' ? maxSize() : maxSize
    newSize = Math.max(minSize, Math.min(max, newSize))
    
    size.value = newSize
    onSizeChange?.(newSize)
  }

  const handleMouseUp = () => {
    if (isResizing.value) {
      isResizing.value = false
      // Remove resizing classes from body
      document.body.classList.remove('resizing-col', 'resizing-row')
      onResizeEnd?.()
    }
  }

  const startResize = (event: MouseEvent) => {
    isResizing.value = true
    startPos = direction === 'horizontal' ? event.clientX : event.clientY
    startSize = size.value
    // Add resizing class to body
    document.body.classList.add(direction === 'horizontal' ? 'resizing-col' : 'resizing-row')
    onResizeStart?.()
  }

  const resizeByDelta = (delta: number) => {
    const max = typeof maxSize === 'function' ? maxSize() : maxSize
    let newSize = reverse ? size.value - delta : size.value + delta
    newSize = Math.max(minSize, Math.min(max, newSize))
    size.value = newSize
    onSizeChange?.(newSize)
  }

  const onKeydown = (event: KeyboardEvent) => {
    const step = event.shiftKey ? 40 : 10
    if (direction === 'horizontal') {
      if (event.key === 'ArrowLeft') {
        event.preventDefault()
        resizeByDelta(reverse ? step : -step)
      } else if (event.key === 'ArrowRight') {
        event.preventDefault()
        resizeByDelta(reverse ? -step : step)
      }
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      resizeByDelta(reverse ? step : -step)
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      resizeByDelta(reverse ? -step : step)
    }
  }

  // Add global event listeners when resizing starts
  onMounted(() => {
    // Listen for resize events globally
    window.addEventListener('mouseup', handleMouseUp)
  })

  onUnmounted(() => {
    window.removeEventListener('mouseup', handleMouseUp)
  })

  // Update mouse move listener when resizing state changes
  onMounted(() => {
    const updateMouseMoveListener = () => {
      if (isResizing.value) {
        window.addEventListener('mousemove', handleMouseMove)
      } else {
        window.removeEventListener('mousemove', handleMouseMove)
      }
    }

    // Watch for changes in isResizing
    const stopWatch = watch(isResizing, updateMouseMoveListener, { immediate: true })
    
    onUnmounted(() => {
      stopWatch()
      window.removeEventListener('mousemove', handleMouseMove)
    })
  })

  return {
    size,
    isResizing,
    startResize,
    resizeByDelta,
    onKeydown,
  }
}

/**
 * Convenience function for horizontal resize (left-side panels)
 */
export function useHorizontalResize(
  initialSize: number, 
  minSize: number = 100, 
  maxSize: number | (() => number) = 800,
  reverse: boolean = false
): UseResizeReturn {
  return useResize({
    initialSize,
    minSize,
    maxSize,
    direction: 'horizontal',
    reverse
  })
}

/**
 * Convenience function for vertical resize
 */
export function useVerticalResize(
  initialSize: number, 
  minSize: number = 40, 
  maxSize: number = 500,
  reverse: boolean = false
): UseResizeReturn {
  return useResize({
    initialSize,
    minSize,
    maxSize,
    direction: 'vertical',
    reverse
  })
}