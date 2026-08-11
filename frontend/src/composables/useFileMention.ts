/**
 * Shared composable for cross-component file mention communication.
 *
 * Allows the file tree context menu (SidebarPanel) to signal ChatPanel that
 * the user wants to reference a file in the conversation. When ChatPanel
 * detects a pending mention, it inserts @file/path into the input and
 * focuses the textarea.
 *
 * Usage:
 *   // In SidebarPanel (producer)
 *   const { requestMention } = useFileMention()
 *   requestMention('models/staging/stg_orders.sql')
 *
 *   // In ChatPanel (consumer)
 *   const { pendingMention, consumeMention } = useFileMention()
 *   watch(pendingMention, (path) => {
 *     if (path) insertMention(consumeMention())
 *   })
 */
import { ref } from 'vue'

const pendingMention = ref<string | null>(null)

export function useFileMention() {
  /** Signal that a file should be mentioned in the chat input. */
  function requestMention(filePath: string) {
    pendingMention.value = filePath
  }

  /** Consume the pending mention (returns the path and clears it). */
  function consumeMention(): string | null {
    const path = pendingMention.value
    pendingMention.value = null
    return path
  }

  return { pendingMention, requestMention, consumeMention }
}
