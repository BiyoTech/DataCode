/**
 * Composable: useLineageLayout
 *
 * Converts a lineage graph (adjacency map + node metadata) into G6-compatible
 * nodes and edges, using dagre for hierarchical left-to-right layout.
 *
 * Graph semantics: graph[fqn] = [upstream_deps]
 *   → edge direction: upstream → downstream (parent → child)
 *   → dagre rankdir: 'LR' (left to right)
 */
import dagre from 'dagre'
import type { ModelGraph, LineageNodeMap } from '@/types/models'
import { NODE_WIDTH, computeNodeHeight } from '@/components/models/lineageNodeRegister'

export interface G6LineageNodeData {
  id: string
  x: number
  y: number
  label: string
  modelType: string
  kind: string
  columns: Array<{ name: string; type: string }>
  isSelected: boolean
  // Per-node view state driving in-node search + pagination.
  page: number
  search: string
  // Column names highlighted as part of an active column-level lineage trace.
  activeColumns: string[]
  // G6 requires type to match registered node name
  type: string
}

export interface G6LineageEdgeData {
  source: string
  target: string
  type: string
  style: Record<string, unknown>
}

export interface G6LineageData {
  nodes: G6LineageNodeData[]
  edges: G6LineageEdgeData[]
}

/** Per-node view state supplied by the component (page/search/active columns). */
export interface LineageViewState {
  pageMap?: Record<string, number>
  searchMap?: Record<string, string>
  activeColumnsMap?: Record<string, Set<string>>
}

/**
 * Transform lineage graph + node metadata into G6 nodes/edges with dagre layout.
 *
 * @param graph        Adjacency map: { fqn: [upstream_deps] }
 * @param nodes        Per-node metadata: { fqn: { name, fqn, type, kind, columns } }
 * @param selectedFqn  The currently selected model FQN (for highlight).
 * @param view         Per-node page/search/active-column state.
 */
export function useLineageLayout(
  graph: ModelGraph,
  nodes: LineageNodeMap,
  selectedFqn: string | null,
  view: LineageViewState = {},
): G6LineageData {
  const pageMap = view.pageMap || {}
  const searchMap = view.searchMap || {}
  const activeColumnsMap = view.activeColumnsMap || {}

  // Collect all unique node IDs from both keys and values
  const allIds = new Set<string>()
  for (const [id, deps] of Object.entries(graph)) {
    allIds.add(id)
    deps.forEach((d: string) => allIds.add(d))
  }

  // Initialize dagre graph — use `as any` for options that @types/dagre is strict about
  const dg = new dagre.graphlib.Graph()
  dg.setGraph({
    rankdir: 'LR',
    nodesep: 40,
    ranksep: 80,
    marginx: 20,
    marginy: 20,
  } as any)
  ;(dg as any).setDefaultEdgeLabel(() => ({}))

  // Add nodes to dagre
  const nodeMeta: Record<
    string,
    {
      columns: Array<{ name: string; type: string }>
      name: string
      type: string
      kind: string
      page: number
      search: string
    }
  > = {}
  for (const id of allIds) {
    const meta = nodes[id]
    const columns = meta?.columns || []
    const name = meta?.name || id.split('.').pop() || id
    const type = meta?.type || 'sql'
    const kind = meta?.kind || ''
    const page = pageMap[id] || 0
    const search = searchMap[id] || ''
    nodeMeta[id] = { columns, name, type, kind, page, search }

    dg.setNode(id, {
      width: NODE_WIDTH,
      height: computeNodeHeight(columns, page, search),
    })
  }

  // Add edges: upstream → downstream (parent → child)
  // graph[fqn] = [upstream_deps], so for each fqn, edge goes from dep → fqn
  const edgeSet = new Set<string>()
  for (const [id, deps] of Object.entries(graph)) {
    for (const dep of deps) {
      const edgeKey = `${dep}->${id}`
      if (dep !== id && !edgeSet.has(edgeKey)) {
        edgeSet.add(edgeKey)
        dg.setEdge(dep, id)
      }
    }
  }

  // Run layout
  dagre.layout(dg)

  // Build G6 node data
  const g6Nodes: G6LineageNodeData[] = Array.from(allIds).map((id: string) => {
    const dagreNode = dg.node(id) as any
    const meta = nodeMeta[id]
    // Match by both FQN (graph key, e.g. '"nova"."instance_wide"') and by
    // model name (e.g. 'nova.instance_wide'), since selectedModelName may
    // hold either form depending on how the model was selected.
    const isSelected = id === selectedFqn || meta.name === selectedFqn
    const height = computeNodeHeight(meta.columns, meta.page, meta.search)
    const activeColumns = Array.from(activeColumnsMap[id] || [])
    return {
      id,
      x: dagreNode ? dagreNode.x - NODE_WIDTH / 2 : 0,
      y: dagreNode ? dagreNode.y - height / 2 : 0,
      label: meta.name,
      modelType: meta.type,
      kind: meta.kind,
      columns: meta.columns,
      isSelected,
      page: meta.page,
      search: meta.search,
      activeColumns,
      type: 'model-lineage-node',
    }
  })

  // Build G6 edge data (table-level)
  const g6Edges: G6LineageEdgeData[] = []
  for (const edgeKey of edgeSet) {
    const [source, target] = edgeKey.split('->')
    g6Edges.push({
      source,
      target,
      type: 'cubic-horizontal',
      style: {
        stroke: '#C9CDD4',
        lineWidth: 1.5,
        endArrow: {
          path: 'M 4,0 L -4,-4 L -4,4 Z',
          fill: '#C9CDD4',
        },
        radius: 12,
      },
    })
  }

  return { nodes: g6Nodes, edges: g6Edges }
}
