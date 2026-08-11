/**
 * Composable: useAssetDagLayout
 *
 * Converts an AssetDagGraph (explicit node list + directed edges) into
 * G6-compatible nodes and edges, using dagre for hierarchical left-to-right
 * layout.
 *
 * Graph semantics (differs from lineage's adjacency map):
 *   edges: [{ source, target }] where source -> target means
 *   upstream -> downstream (producer -> consumer).
 *   → dagre rankdir: 'LR' (left to right)
 *
 * Nodes are fixed-height cards (ASSET_NODE_WIDTH x ASSET_CARD_HEIGHT), so no
 * per-node height computation is required.
 */
import dagre from 'dagre'
import type { AssetDagGraph } from '@/types/dagster'
import { ASSET_NODE_WIDTH, ASSET_CARD_HEIGHT } from '@/components/asset-dag/assetDagNodeRegister'

export interface G6AssetNodeData {
  id: string
  x: number
  y: number
  label: string
  assetType: string
  groupName: string
  runStatus: string
  depsCount: number
  isSelected: boolean
  // G6 requires `type` to match the registered node name.
  type: string
}

export interface G6AssetEdgeData {
  source: string
  target: string
  type: string
  style: Record<string, unknown>
}

export interface G6AssetDagData {
  nodes: G6AssetNodeData[]
  edges: G6AssetEdgeData[]
}

/** Optional per-asset run status map: { asset_key_str: run_status }. */
export type AssetRunStatusMap = Record<string, string>

/**
 * Transform an AssetDagGraph into G6 nodes/edges with dagre LR layout.
 *
 * @param graph          Nodes (AssetInfo[]) + directed edges (source -> target).
 * @param selectedKey    Currently selected asset_key_str (for highlight).
 * @param runStatusMap   Optional latest run status per asset key.
 */
export function useAssetDagLayout(
  graph: AssetDagGraph,
  selectedKey: string | null,
  runStatusMap: AssetRunStatusMap = {},
): G6AssetDagData {
  const nodes = graph.nodes || []
  const edges = graph.edges || []

  // Collect all unique node IDs from both the node list and edge endpoints,
  // guarding against edges that reference assets missing from `nodes`.
  const allIds = new Set<string>()
  for (const n of nodes) allIds.add(n.asset_key_str)
  for (const e of edges) {
    allIds.add(e.source)
    allIds.add(e.target)
  }

  // Index node metadata by key for O(1) lookup during layout.
  const metaById = new Map(nodes.map((n) => [n.asset_key_str, n]))

  // Initialize dagre graph.
  const dg = new dagre.graphlib.Graph()
  dg.setGraph({
    rankdir: 'LR',
    nodesep: 32,
    ranksep: 90,
    marginx: 24,
    marginy: 24,
  } as any)
  ;(dg as any).setDefaultEdgeLabel(() => ({}))

  // Add nodes (fixed-size cards).
  for (const id of allIds) {
    dg.setNode(id, { width: ASSET_NODE_WIDTH, height: ASSET_CARD_HEIGHT })
  }

  // Add edges: source -> target (upstream -> downstream), dedup self/repeat.
  const edgeSet = new Set<string>()
  for (const e of edges) {
    if (e.source === e.target) continue
    const key = `${e.source}->${e.target}`
    if (edgeSet.has(key)) continue
    edgeSet.add(key)
    dg.setEdge(e.source, e.target)
  }

  // Run layout.
  dagre.layout(dg)

  // Build G6 node data (dagre positions node centers; G6 x/y is top-left).
  const g6Nodes: G6AssetNodeData[] = Array.from(allIds).map((id) => {
    const dagreNode = dg.node(id) as any
    const meta = metaById.get(id)
    // Prefer the SQLMesh model display name (`schema.table`, matching
    // /api/v1/sqlmesh/models) when present; fall back to the asset key string.
    const label = meta?.name || meta?.asset_key_str || id.split('/').pop() || id
    return {
      id,
      x: dagreNode ? dagreNode.x - ASSET_NODE_WIDTH / 2 : 0,
      y: dagreNode ? dagreNode.y - ASSET_CARD_HEIGHT / 2 : 0,
      label,
      assetType: meta?.kind || 'data',
      groupName: meta?.group_name || '',
      runStatus: runStatusMap[id] || 'none',
      depsCount: meta?.deps?.length ?? 0,
      isSelected: id === selectedKey,
      type: 'asset-dag-node',
    }
  })

  // Build G6 edge data with neon-accented curves.
  const g6Edges: G6AssetEdgeData[] = Array.from(edgeSet).map((key) => {
    const [source, target] = key.split('->')
    return {
      source,
      target,
      type: 'cubic-horizontal',
      style: {
        stroke: 'rgba(99, 102, 241, 0.55)',
        lineWidth: 1.5,
        endArrow: {
          path: 'M 4,0 L -4,-4 L -4,4 Z',
          fill: 'rgba(99, 102, 241, 0.55)',
        },
        radius: 12,
      },
    }
  })

  return { nodes: g6Nodes, edges: g6Edges }
}
