/**
 * Central workspace switch orchestration.
 *
 * Resets workspace-scoped Pinia stores when the active workspace changes so
 * panels do not leak data across workspaces without a full page reload.
 */
import { watch } from 'vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { useFileStore } from '@/stores/files'
import { useAdhocStore } from '@/stores/adhoc'
import { useModelsStore } from '@/stores/models'
import { useOntologyStore } from '@/stores/ontology'
import { usePlanStore } from '@/stores/plan'
import { useExternalModelsStore } from '@/stores/externalModels'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useGitStore } from '@/stores/git'
import { useEnvironmentStore } from '@/stores/environment'
import { useLogsStore } from '@/stores/logs'
import { useWipStore } from '@/stores/wip'

export function useWorkspaceLifecycle() {
  const workspaceStore = useWorkspaceStore()
  const fileStore = useFileStore()
  const adhoc = useAdhocStore()
  const models = useModelsStore()
  const ontology = useOntologyStore()
  const plan = usePlanStore()
  const externalModels = useExternalModelsStore()
  const knowledge = useKnowledgeStore()
  const git = useGitStore()
  const environment = useEnvironmentStore()
  const logs = useLogsStore()
  const wip = useWipStore()

  function resetWorkspaceScopedState() {
    fileStore.reset()
    adhoc.reset()
    models.reset()
    ontology.reset()
    plan.reset()
    plan.resetHistory()
    externalModels.reset()
    knowledge.reset()
    git.reset()
    environment.reset()
    // Also tears down the SSE tail: it is bound to the previous working copy.
    logs.reset()
    // Stops the WIP status polling bound to the previous working copy.
    wip.reset()
  }

  watch(
    () => workspaceStore.currentWorkspace?.id ?? null,
    (workspaceId, previousId) => {
      if (!workspaceId || workspaceId === previousId) return
      resetWorkspaceScopedState()
    },
  )
}
