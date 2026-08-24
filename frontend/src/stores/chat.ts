/**
 * Chat store — development-assistant scenario.
 */
import { createChatStore } from '@/stores/createChatStore'

export const useChatStore = createChatStore({
  storeId: 'chat',
  lsSessionKey: 'datacode_current_session_id',
  defaultMode: 'craft',
  defaultExecutionStyle: 'agent',
  enableTypewriter: true,
  enableModeSwitch: true,
})
