/**
 * ChatBI store — analyst scenario with independent session pool.
 */
import { createChatStore } from '@/stores/createChatStore'

export const useChatbiStore = createChatStore({
  storeId: 'chatbi',
  lsSessionKey: 'datacode_chatbi_session_id',
  scenario: 'chatbi',
  defaultMode: 'craft',
  defaultExecutionStyle: 'agent',
  enableTypewriter: false,
  enableModeSwitch: false,
})
