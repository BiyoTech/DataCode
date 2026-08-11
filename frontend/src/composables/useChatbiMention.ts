/**
 * useChatbiMention — multi-source @-mention composable for the ChatBI scenario.
 *
 * Aggregates three data sources into a unified, grouped suggestion list:
 *   1. Tables:     project-space SQLMesh model catalog (adhocStore.tree flattened)
 *   2. Knowledge:  business knowledge documents (knowledgeStore.documents)
 *   3. Adhoc:      saved SQL query files (adhocStore.adhocFiles)
 *
 * Data sources are lazy-loaded in parallel on the first `@` trigger and cached
 * afterwards (the underlying stores retain their cached state).
 */
import { ref } from 'vue'
import { useAdhocStore, type CatalogNode } from '@/stores/adhoc'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useWorkspaceStore } from '@/stores/workspace'
import { notifyError } from '@/utils/notifyError'

/** Mention category — each maps to a data source. */
export type MentionType = 'table' | 'knowledge' | 'adhoc'

/** A single mentionable item, unified across all data sources. */
export interface MentionItem {
  type: MentionType
  /** table: fqn | knowledge: docId | adhoc: filePath */
  id: string
  /** Display name (table name, document title, query display name) */
  label: string
  /** Secondary description (FQN / content_type·size / file path) */
  subtitle: string
  /** TDesign icon name */
  icon: string
  /** Inserted token, e.g. @table:sales.orders */
  token: string
}

/** A grouped suggestion result — one group per MentionType. */
export interface MentionGroup {
  type: MentionType
  items: MentionItem[]
}

/** Maximum items returned per group to keep the popup scannable. */
const MAX_PER_GROUP = 8

export function useChatbiMention() {
  const adhocStore = useAdhocStore()
  const knowledgeStore = useKnowledgeStore()
  const workspaceStore = useWorkspaceStore()

  const loading = ref(false)
  const loaded = ref(false)

  /**
   * Lazy-load all three data sources on first `@` trigger.
   * Subsequent calls are no-ops once `loaded` is true.
   */
  async function ensureLoaded(): Promise<void> {
    if (loaded.value) return
    const ws = workspaceStore.currentWorkspace
    if (!ws) return

    loading.value = true
    try {
      await Promise.all([
        adhocStore.loadCatalog(),
        knowledgeStore.fetchDocuments(),
        adhocStore.listAdhocFiles(),
      ])
      loaded.value = true
    } catch (e) {
      notifyError(e, 'Failed to load mention sources')
    } finally {
      loading.value = false
    }
  }

  /** Reset the loaded flag (e.g. on workspace switch). */
  function reset(): void {
    loaded.value = false
  }

  // ─────────── Source builders ───────────

  /** Flatten the catalog tree into table mention items. */
  function buildTableItems(): MentionItem[] {
    const items: MentionItem[] = []
    function walk(nodes: CatalogNode[]) {
      for (const node of nodes) {
        if (node.type === 'table' && node.fqn) {
          items.push({
            type: 'table',
            id: node.fqn,
            label: node.name,
            subtitle: node.fqn,
            icon: 'server',
            token: `@table:${node.fqn}`,
          })
        }
        if (node.children?.length) {
          walk(node.children)
        }
      }
    }
    walk(adhocStore.tree)
    return items
  }

  /** Build knowledge mention items from store documents. */
  function buildKnowledgeItems(): MentionItem[] {
    return knowledgeStore.documents.map((doc) => ({
      type: 'knowledge' as const,
      id: doc.id,
      label: doc.title,
      subtitle: `${doc.content_type} · ${_formatSize(doc.file_size)}`,
      icon: 'book',
      token: `@knowledge:${doc.id}:${doc.title}`,
    }))
  }

  /** Build adhoc mention items from store adhoc files. */
  function buildAdhocItems(): MentionItem[] {
    return adhocStore.adhocFiles.map((file) => ({
      type: 'adhoc' as const,
      id: file.path,
      label: file.displayName,
      subtitle: file.path,
      icon: 'file-paste',
      token: `@adhoc:${file.path}`,
    }))
  }

  // ─────────── Search ───────────

  /**
   * Search across all data sources, returning grouped results filtered by query.
   * Each group is capped at MAX_PER_GROUP items.
   */
  function search(query: string): MentionGroup[] {
    const q = query.toLowerCase().trim()
    const tables = _filterItems(buildTableItems(), q).slice(0, MAX_PER_GROUP)
    const knowledge = _filterItems(buildKnowledgeItems(), q).slice(0, MAX_PER_GROUP)
    const adhoc = _filterItems(buildAdhocItems(), q).slice(0, MAX_PER_GROUP)

    return [
      { type: 'table', items: tables },
      { type: 'knowledge', items: knowledge },
      { type: 'adhoc', items: adhoc },
    ].filter((g) => g.items.length > 0)
  }

  /** Filter items by matching query against label + subtitle + id. */
  function _filterItems(items: MentionItem[], q: string): MentionItem[] {
    if (!q) return items
    return items.filter(
      (item) =>
        item.label.toLowerCase().includes(q) ||
        item.subtitle.toLowerCase().includes(q) ||
        item.id.toLowerCase().includes(q),
    )
  }

  /** Human-readable file size for the knowledge subtitle. */
  function _formatSize(bytes: number): string {
    if (!bytes || bytes <= 0) return '0B'
    const units = ['B', 'KB', 'MB']
    let size = bytes
    let unitIdx = 0
    while (size >= 1024 && unitIdx < units.length - 1) {
      size /= 1024
      unitIdx++
    }
    return `${Math.round(size)}${units[unitIdx]}`
  }

  return {
    loading,
    loaded,
    ensureLoaded,
    reset,
    search,
  }
}
