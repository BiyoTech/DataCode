/**
 * Ontology store — manages the list of user-authored ontologies and the full
 * semantic model for the active workspace.
 *
 * - The sidebar list is derived from `{workspace}/ontology/*.yaml` via the
 *   files API (no dedicated backend route needed for a flat listing).
 * - The detail view fetches the fully-parsed semantic model (entities,
 *   relationships, metrics, AI context + validation) from the ontology-service
 *   via `GET /api/v1/ontology/{name}`.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '@/utils/api'
import type {
  OntologySummary,
  SemanticModel,
  Relationship,
  OntologyModelResult,
} from '@/types/ontology'

// Files returned by the files API come back as POSIX-style relative paths.
const ONTOLOGY_DIR = 'ontology'

export const useOntologyStore = defineStore('ontology', () => {
  // ─────────── State ───────────
  const ontologies = ref<OntologySummary[]>([])
  const isLoading = ref(false)
  const selectedOntologyName = ref<string | null>(null)
  // Cached per workspace so we don't re-scan on tab toggles.
  let loadedWorkspacePath: string | null = null

  // ─────────── Actions ───────────

  /**
   * List ontologies for the given workspace.
   * Safe to call repeatedly — the result is cached per workspace path.
   */
  async function fetchOntologies(workspacePath: string): Promise<void> {
    if (!workspacePath) return
    if (loadedWorkspacePath === workspacePath) {
      // Already scanned this workspace — keep the cache.
      return
    }

    isLoading.value = true
    try {
      const { data } = await api.get('/files/list', {
        params: { workspace_path: workspacePath, directory: ONTOLOGY_DIR, recursive: false },
      })
      const files: { path: string; is_directory: boolean }[] = data.files || []
      ontologies.value = files
        .filter((f) => !f.is_directory && /\.ya?ml$/i.test(f.path))
        .map((f) => _summaryFromPath(f.path))
        .sort((a, b) => a.name.localeCompare(b.name))
      loadedWorkspacePath = workspacePath
    } catch (e) {
      // A missing ontology/ directory is the normal "no ontologies yet" case —
      // any other error is logged but never thrown to the UI.
      console.warn('[ontology] Failed to list ontology files:', e)
      ontologies.value = []
      loadedWorkspacePath = workspacePath
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Clear cache when switching workspaces so a fresh scan runs.
   */
  function reset(): void {
    ontologies.value = []
    selectedOntologyName.value = null
    loadedWorkspacePath = null
  }

  function selectOntology(name: string | null): void {
    selectedOntologyName.value = name
  }

  /**
   * Fetch the fully-parsed semantic model for the detail view.
   *
   * `check_sqlmesh=false` keeps this fast and independent of the sqlmesh-service:
   * the detail view only needs structural validation, not cross-service existence
   * checks. Returns `null` on 404 (ontology not found) so callers can show a
   * dedicated "not found" state; other errors are re-thrown.
   */
  async function fetchOntologyModel(
    workspacePath: string,
    name: string,
  ): Promise<OntologyModelResult | null> {
    const { data } = await api
      .get<OntologyModelResult>(`/ontology/${encodeURIComponent(name)}`, {
        params: { workspace_path: workspacePath, check_sqlmesh: false },
      })
      .catch((e) => {
        if (e?.response?.status === 404) return { data: null }
        throw e
      })

    if (!data) return null
    // Normalise the wire shape (relationships use aliased `from` / `to`).
    data.model = _normaliseModel(data.model)
    return data
  }

  return {
    // State
    ontologies,
    isLoading,
    selectedOntologyName,
    // Actions
    fetchOntologies,
    selectOntology,
    fetchOntologyModel,
    reset,
  }
})

// ─────────── Internal helpers ───────────

/**
 * Build an OntologySummary from a file path returned by the files API.
 * The file stem (without `.yaml`) is the ontology name.
 */
function _summaryFromPath(path: string): OntologySummary {
  const fileName = path.split('/').pop() || path
  const name = fileName.replace(/\.ya?ml$/i, '')
  return { name, path, description: '' }
}

/**
 * Normalise a raw semantic model from the API into the UI-facing shape.
 * The only fix-up needed is mapping the backend's aliased relationship keys
 * (`from` / `to`) onto `from_entity` / `to_entity`.
 */
function _normaliseModel(model: SemanticModel): SemanticModel {
  const relationships: Record<string, Relationship> = {}
  for (const [key, rel] of Object.entries(model.relationships || {})) {
    const raw = rel as Relationship & { from?: string; to?: string }
    relationships[key] = {
      ...raw,
      from_entity: raw.from_entity ?? raw.from ?? '',
      to_entity: raw.to_entity ?? raw.to ?? '',
    }
  }
  return { ...model, relationships }
}
