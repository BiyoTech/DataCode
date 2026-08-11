/**
 * G6 custom node registration for lineage DAG visualization.
 *
 * Registers a 'model-lineage-node' node type that renders a card with:
 * - Header: colored bar with a clickable model-name hyperlink + column count badge
 * - Optional search box (when the model has more columns than one page)
 * - Body: a paginated column list (PAGE_SIZE rows per page)
 * - Optional pager row ("‹  1/N  ›") when the filtered result spans multiple pages
 * - Column rows are individually selectable to trace column-level lineage
 *
 * Interaction targets are identified by shape `name`:
 * - 'title-link' / 'title-underline' → open model detail drawer
 * - 'search-box' / 'search-text'     → open the floating search input
 * - 'pager-prev' / 'pager-next'      → change the current page
 * - 'colrow::<columnName>'           → toggle column selection
 */
import G6 from '@antv/g6'

// ── Layout constants — tuned for TDesign 12px body font ──
export const NODE_WIDTH = 280
export const HEADER_HEIGHT = 36
export const SEARCH_HEIGHT = 34
export const ROW_HEIGHT = 26
export const PAGER_HEIGHT = 28
export const PADDING = 4
// Columns rendered per page. Lists longer than this get search + pagination.
export const PAGE_SIZE = 10

// Type-based color mapping for the header bar
const TYPE_COLORS: Record<string, string> = {
  sql: '#0052D9',
  incremental: '#366EF4',
  view: '#00A870',
  seed: '#F2994A',
  external: '#86909C',
  python: '#7B61FF',
}

interface Column {
  name: string
  type: string
}

/** Resolve header color by model type/kind. */
function resolveHeaderColor(type: string, kind: string): string {
  if (kind && TYPE_COLORS[kind.toLowerCase()]) return TYPE_COLORS[kind.toLowerCase()]
  if (type && TYPE_COLORS[type.toLowerCase()]) return TYPE_COLORS[type.toLowerCase()]
  return '#0052D9'
}

/** Truncate long labels with ellipsis. */
function truncateLabel(label: string, maxLen: number): string {
  if (!label) return ''
  return label.length > maxLen ? label.slice(0, maxLen) + '…' : label
}

/**
 * Resolve the visible columns for a node given its current page and search
 * query. Centralized here so the node renderer and the column-edge overlay
 * stay perfectly in sync about which rows are shown and where.
 */
export function getVisibleColumns(
  columns: Column[],
  page: number,
  search: string,
): { visible: Column[]; filteredCount: number; totalPages: number; safePage: number } {
  const q = (search || '').trim().toLowerCase()
  const filtered = q ? columns.filter((c) => (c.name || '').toLowerCase().includes(q)) : columns
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const safePage = Math.min(Math.max(0, page || 0), totalPages - 1)
  const start = safePage * PAGE_SIZE
  return {
    visible: filtered.slice(start, start + PAGE_SIZE),
    filteredCount: filtered.length,
    totalPages,
    safePage,
  }
}

/** Whether the search box should be rendered for a column list. */
function shouldShowSearch(columns: Column[]): boolean {
  return columns.length > PAGE_SIZE
}

/** Y offset (from the node top) where the first column row begins. */
function columnsTop(columns: Column[]): number {
  return HEADER_HEIGHT + (shouldShowSearch(columns) ? SEARCH_HEIGHT : 0)
}

/**
 * Compute the rendered height of a node card for the given page/search state.
 */
export function computeNodeHeight(columns: Column[], page: number, search: string): number {
  const { visible, filteredCount } = getVisibleColumns(columns, page, search)
  const rowCount = filteredCount === 0 ? 1 : visible.length // "no match" placeholder row
  const showPager = filteredCount > PAGE_SIZE
  return columnsTop(columns) + rowCount * ROW_HEIGHT + (showPager ? PAGER_HEIGHT : 0) + PADDING
}

/**
 * Geometry of a specific column row for the current page/search state.
 * Returns whether the column is currently visible and, if so, its row-center
 * Y offset relative to the node top — used to anchor column-level edges.
 */
export function getColumnRowGeometry(
  columns: Column[],
  page: number,
  search: string,
  colName: string,
): { visible: boolean; y: number } {
  const { visible } = getVisibleColumns(columns, page, search)
  const idx = visible.findIndex((c) => c.name === colName)
  if (idx < 0) return { visible: false, y: 0 }
  return { visible: true, y: columnsTop(columns) + idx * ROW_HEIGHT + ROW_HEIGHT / 2 }
}

/** Track whether the custom node has already been registered. */
let registered = false

/**
 * Register the 'model-lineage-node' custom node with G6.
 * Safe to call multiple times — only registers once.
 */
export function registerLineageNode(): void {
  if (registered) return
  registered = true

  G6.registerNode(
    'model-lineage-node',
    {
      options: {
        style: {},
        stateStyles: {
          selected: {
            stroke: '#F2994A',
            lineWidth: 3,
            shadowColor: 'rgba(242, 153, 74, 0.35)',
            shadowBlur: 12,
          },
          hover: {
            stroke: '#366EF4',
            lineWidth: 2,
            shadowColor: 'rgba(54, 110, 244, 0.2)',
            shadowBlur: 8,
          },
        },
      },

      draw(cfg: any, group: any) {
        const { label, modelType, kind, isSelected } = cfg
        const colList: Column[] = cfg.columns || []
        const page: number = cfg.page || 0
        const search: string = cfg.search || ''
        const activeSet = new Set<string>(cfg.activeColumns || [])
        const headerColor = resolveHeaderColor(modelType || '', kind || '')

        const showSearch = shouldShowSearch(colList)
        const { visible, filteredCount, totalPages, safePage } = getVisibleColumns(
          colList,
          page,
          search,
        )
        const showPager = filteredCount > PAGE_SIZE
        const totalHeight = computeNodeHeight(colList, page, search)
        const radius = 8

        // ── Card background (key shape) ──
        const keyShape = group.addShape('rect', {
          attrs: {
            x: 0,
            y: 0,
            width: NODE_WIDTH,
            height: totalHeight,
            fill: '#FFFFFF',
            stroke: isSelected ? '#F2994A' : '#E5E6EB',
            lineWidth: isSelected ? 2 : 1,
            radius,
            shadowColor: isSelected ? 'rgba(242, 153, 74, 0.3)' : 'rgba(0, 0, 0, 0.06)',
            shadowBlur: isSelected ? 12 : 4,
          },
          name: 'card-body',
          draggable: true,
        })

        // ── Header bar (colored) ──
        group.addShape('rect', {
          attrs: {
            x: 0,
            y: 0,
            width: NODE_WIDTH,
            height: HEADER_HEIGHT,
            fill: headerColor,
            radius: [radius, radius, 0, 0],
          },
          name: 'card-header',
          draggable: true,
        })

        // ── Model name as a hyperlink (white text + underline) ──
        const titleText = truncateLabel(label || '', 30)
        group.addShape('text', {
          attrs: {
            x: 12,
            y: HEADER_HEIGHT / 2 + 4,
            text: titleText,
            fontSize: 13,
            fontWeight: 600,
            fill: '#FFFFFF',
            fontFamily: 'PingFang SC, sans-serif',
            cursor: 'pointer',
          },
          name: 'title-link',
          draggable: false,
        })
        // Approximate underline to signal the link affordance.
        const underlineWidth = Math.min(titleText.length * 7.4, NODE_WIDTH - 60)
        group.addShape('line', {
          attrs: {
            x1: 12,
            y1: HEADER_HEIGHT / 2 + 9,
            x2: 12 + underlineWidth,
            y2: HEADER_HEIGHT / 2 + 9,
            stroke: 'rgba(255, 255, 255, 0.85)',
            lineWidth: 1,
            cursor: 'pointer',
          },
          name: 'title-underline',
          draggable: false,
        })

        // ── Column count badge (right side of header) ──
        const badgeText = `${colList.length}`
        const badgeWidth = 28
        const badgeX = NODE_WIDTH - badgeWidth - 8
        group.addShape('rect', {
          attrs: {
            x: badgeX,
            y: 8,
            width: badgeWidth,
            height: 20,
            fill: 'rgba(255, 255, 255, 0.25)',
            radius: 10,
          },
          name: 'badge-bg',
          draggable: true,
        })
        group.addShape('text', {
          attrs: {
            x: badgeX + badgeWidth / 2,
            y: 22,
            text: badgeText,
            fontSize: 11,
            fontWeight: 600,
            fill: '#FFFFFF',
            textAlign: 'center',
            fontFamily: 'PingFang SC, sans-serif',
          },
          name: 'badge-text',
          draggable: true,
        })

        // ── Search box (only when column list needs pagination) ──
        if (showSearch) {
          const boxY = HEADER_HEIGHT + 5
          const boxH = SEARCH_HEIGHT - 10
          group.addShape('rect', {
            attrs: {
              x: 10,
              y: boxY,
              width: NODE_WIDTH - 20,
              height: boxH,
              fill: '#F2F3F5',
              stroke: '#E5E6EB',
              lineWidth: 1,
              radius: 6,
              cursor: 'text',
            },
            name: 'search-box',
            draggable: false,
          })
          const hasQuery = !!(search && search.trim())
          group.addShape('text', {
            attrs: {
              x: 20,
              y: boxY + boxH / 2 + 4,
              text: truncateLabel(hasQuery ? search : '⌕  Search fields…', 34),
              fontSize: 12,
              fill: hasQuery ? '#1D2129' : '#A9AEB8',
              fontFamily: 'PingFang SC, sans-serif',
              cursor: 'text',
            },
            name: 'search-text',
            draggable: false,
          })
        }

        const rowsTop = columnsTop(colList)

        // ── Empty search result placeholder ──
        if (filteredCount === 0) {
          group.addShape('text', {
            attrs: {
              x: NODE_WIDTH / 2,
              y: rowsTop + ROW_HEIGHT / 2 + 4,
              text: 'No matching fields',
              fontSize: 12,
              fill: '#A9AEB8',
              textAlign: 'center',
              fontFamily: 'PingFang SC, sans-serif',
            },
            name: 'empty-row',
            draggable: true,
          })
        }

        // ── Column rows (current page) ──
        visible.forEach((col, i) => {
          const rowY = rowsTop + i * ROW_HEIGHT
          const isActive = activeSet.has(col.name)

          // Row background: active (orange tint) > alternating stripe
          if (isActive) {
            group.addShape('rect', {
              attrs: { x: 1, y: rowY, width: NODE_WIDTH - 2, height: ROW_HEIGHT, fill: '#FFF3E8' },
              name: `rowbg-active-${i}`,
              draggable: true,
            })
          } else if (i % 2 === 1) {
            group.addShape('rect', {
              attrs: { x: 0, y: rowY, width: NODE_WIDTH, height: ROW_HEIGHT, fill: '#F7F8FA' },
              name: `row-bg-${i}`,
              draggable: true,
            })
          }

          // Column name (left)
          group.addShape('text', {
            attrs: {
              x: 12,
              y: rowY + ROW_HEIGHT / 2 + 4,
              text: truncateLabel(col.name || '', 22),
              fontSize: 12,
              fontWeight: isActive ? 600 : 500,
              fill: isActive ? '#D46B08' : '#1D2129',
              fontFamily: 'JetBrains Mono, monospace',
              cursor: 'pointer',
            },
            name: `colname-${i}`,
            draggable: false,
          })

          // Column type (right, gray)
          group.addShape('text', {
            attrs: {
              x: NODE_WIDTH - 12,
              y: rowY + ROW_HEIGHT / 2 + 4,
              text: truncateLabel(col.type || '', 14),
              fontSize: 11,
              fill: '#86909C',
              textAlign: 'right',
              fontFamily: 'JetBrains Mono, monospace',
              cursor: 'pointer',
            },
            name: `coltype-${i}`,
            draggable: false,
          })

          // Connection anchor dots for active columns (left + right edges)
          if (isActive) {
            const cy = rowY + ROW_HEIGHT / 2
            group.addShape('circle', {
              attrs: { x: 0, y: cy, r: 3.5, fill: '#F2994A' },
              name: `anchor-l-${i}`,
              draggable: false,
            })
            group.addShape('circle', {
              attrs: { x: NODE_WIDTH, y: cy, r: 3.5, fill: '#F2994A' },
              name: `anchor-r-${i}`,
              draggable: false,
            })
          }

          // Transparent hit area for column selection — carries the column name.
          group.addShape('rect', {
            attrs: {
              x: 0,
              y: rowY,
              width: NODE_WIDTH,
              height: ROW_HEIGHT,
              fill: '#000000',
              opacity: 0,
              cursor: 'pointer',
            },
            name: `colrow::${col.name}`,
            draggable: false,
          })
        })

        // ── Pager row ──
        if (showPager) {
          const pagerY = rowsTop + visible.length * ROW_HEIGHT
          const cy = pagerY + PAGER_HEIGHT / 2

          group.addShape('rect', {
            attrs: {
              x: 0,
              y: pagerY,
              width: NODE_WIDTH,
              height: PAGER_HEIGHT,
              fill: '#FAFBFC',
              radius: [0, 0, radius, radius],
            },
            name: 'pager-bg',
            draggable: true,
          })

          const prevEnabled = safePage > 0
          const nextEnabled = safePage < totalPages - 1
          const midX = NODE_WIDTH / 2

          // Prev
          group.addShape('text', {
            attrs: {
              x: midX - 44,
              y: cy + 5,
              text: '‹',
              fontSize: 16,
              fontWeight: 600,
              fill: prevEnabled ? '#366EF4' : '#C9CDD4',
              textAlign: 'center',
              cursor: prevEnabled ? 'pointer' : 'not-allowed',
            },
            name: 'pager-prev',
            draggable: false,
          })
          // Page indicator
          group.addShape('text', {
            attrs: {
              x: midX,
              y: cy + 4,
              text: `${safePage + 1} / ${totalPages}`,
              fontSize: 12,
              fill: '#4E5969',
              textAlign: 'center',
              fontFamily: 'PingFang SC, sans-serif',
            },
            name: 'pager-info',
            draggable: false,
          })
          // Next
          group.addShape('text', {
            attrs: {
              x: midX + 44,
              y: cy + 5,
              text: '›',
              fontSize: 16,
              fontWeight: 600,
              fill: nextEnabled ? '#366EF4' : '#C9CDD4',
              textAlign: 'center',
              cursor: nextEnabled ? 'pointer' : 'not-allowed',
            },
            name: 'pager-next',
            draggable: false,
          })
        }

        return keyShape
      },

      /** Update visual state on hover/select. */
      setState(name?: string, value?: string | boolean, item?: any) {
        if (!item) return
        const shape = item.get('keyShape')
        if (!shape) return

        if (name === 'selected' && value) {
          shape.attr({
            stroke: '#F2994A',
            lineWidth: 2,
            shadowColor: 'rgba(242, 153, 74, 0.3)',
            shadowBlur: 12,
          })
        } else if (name === 'selected' && !value) {
          shape.attr({
            stroke: '#E5E6EB',
            lineWidth: 1,
            shadowColor: 'rgba(0, 0, 0, 0.06)',
            shadowBlur: 4,
          })
        } else if (name === 'hover' && value) {
          shape.attr({
            stroke: '#366EF4',
            lineWidth: 2,
            shadowColor: 'rgba(54, 110, 244, 0.2)',
            shadowBlur: 8,
          })
        } else if (name === 'hover' && !value) {
          // Revert to selected or default state
          const model = item.getModel()
          if (model.isSelected) {
            shape.attr({
              stroke: '#F2994A',
              lineWidth: 2,
              shadowColor: 'rgba(242, 153, 74, 0.3)',
              shadowBlur: 12,
            })
          } else {
            shape.attr({
              stroke: '#E5E6EB',
              lineWidth: 1,
              shadowColor: 'rgba(0, 0, 0, 0.06)',
              shadowBlur: 4,
            })
          }
        }
      },

      /** Anchor points for edge connections: left-center and right-center. */
      getAnchorPoints() {
        return [
          [0, 0.5], // left-center (incoming from upstream)
          [1, 0.5], // right-center (outgoing to downstream)
        ]
      },
    },
    'single-node',
  )
}
