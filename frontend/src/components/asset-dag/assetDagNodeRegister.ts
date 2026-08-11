/**
 * G6 custom node registration for the Asset Dag visualization.
 *
 * Registers an 'asset-dag-node' node type rendered as a compact, fixed-height
 * card (unlike the taller, paginated lineage node). Each card shows:
 * - A type color bar (left edge) keyed by asset type.
 * - The asset name (hyperlink affordance) + group name.
 * - A run-status badge (top-right) driven by the latest run status.
 * - A dependency count chip (bottom-right).
 *
 * The dark-tech aesthetic (deep slate fill + neon accents) intentionally
 * differs from the light lineage cards to distinguish Data+AI pipelines from
 * SQL warehouse lineage.
 */
import G6 from '@antv/g6'

// ── Layout constants (fixed-height card) ──
export const ASSET_NODE_WIDTH = 240
export const ASSET_CARD_HEIGHT = 72
const TYPE_BAR_WIDTH = 5
const RADIUS = 10

// Asset type → neon accent color. Extends SQLMesh types with ML/Feature/Daft.
export const ASSET_TYPE_COLORS: Record<string, string> = {
  sqlmesh: '#06B6D4', // SQLMesh source asset (cyan)
  data: '#6366F1', // Data transformation asset (indigo)
  feature: '#8B5CF6', // Feature engineering asset (violet)
  ml: '#F59E0B', // ML model asset (amber)
  daft: '#10B981', // Daft DataFrame asset (emerald)
}

// Run status → badge color for the status dot.
export const RUN_STATUS_COLORS: Record<string, string> = {
  success: '#10B981',
  failure: '#EF4444',
  running: '#F59E0B',
  started: '#F59E0B',
  queued: '#94A3B8',
  skipped: '#94A3B8',
  canceled: '#94A3B8',
  none: '#475569',
}

/** Resolve the type-bar color, falling back to the data color. */
export function resolveAssetColor(assetType: string): string {
  const key = (assetType || '').toLowerCase()
  return ASSET_TYPE_COLORS[key] || ASSET_TYPE_COLORS.data
}

/**
 * Read a themed color from the app's CSS custom properties so canvas-drawn
 * nodes stay in sync with the active TDesign light/dark theme. Falls back to a
 * sensible default when running outside the DOM (e.g. tests).
 */
function themeColor(varName: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(varName).trim()
  return value || fallback
}

/** Snapshot the themed palette used by the node card. */
export function getNodeTheme() {
  return {
    cardFill: themeColor('--td-bg-color-container', 'rgba(30, 41, 59, 0.92)'),
    cardStroke: themeColor('--td-component-border', 'rgba(148, 163, 184, 0.25)'),
    title: themeColor('--td-text-color-primary', '#F1F5F9'),
    subtitle: themeColor('--td-text-color-secondary', '#94A3B8'),
    muted: themeColor('--td-text-color-placeholder', '#64748B'),
    selected: themeColor('--td-brand-color', '#8B5CF6'),
    hover: themeColor('--td-brand-color-hover', '#6366F1'),
  }
}

/** Resolve the run-status dot color. */
function resolveStatusColor(status: string): string {
  return RUN_STATUS_COLORS[(status || 'none').toLowerCase()] || RUN_STATUS_COLORS.none
}

/** Truncate long labels with an ellipsis. */
function truncate(label: string, maxLen: number): string {
  if (!label) return ''
  return label.length > maxLen ? label.slice(0, maxLen) + '…' : label
}

let registered = false

/**
 * Register the 'asset-dag-node' custom node with G6.
 * Idempotent — safe to call multiple times.
 */
export function registerAssetDagNode(): void {
  if (registered) return
  registered = true

  G6.registerNode(
    'asset-dag-node',
    {
      options: {
        style: {},
        stateStyles: {
          selected: {
            stroke: '#8B5CF6',
            lineWidth: 2.5,
            shadowColor: 'rgba(139, 92, 246, 0.5)',
            shadowBlur: 16,
          },
          hover: {
            stroke: '#6366F1',
            lineWidth: 2,
            shadowColor: 'rgba(99, 102, 241, 0.35)',
            shadowBlur: 10,
          },
        },
      },

      draw(cfg: any, group: any) {
        const {
          label,
          assetType = 'data',
          groupName = '',
          runStatus = 'none',
          depsCount = 0,
          isSelected = false,
        } = cfg
        const accent = resolveAssetColor(assetType)
        const statusColor = resolveStatusColor(runStatus)
        const theme = getNodeTheme()

        // ── Card background (key shape) — follows the active app theme ──
        const keyShape = group.addShape('rect', {
          attrs: {
            x: 0,
            y: 0,
            width: ASSET_NODE_WIDTH,
            height: ASSET_CARD_HEIGHT,
            fill: theme.cardFill,
            stroke: isSelected ? theme.selected : theme.cardStroke,
            lineWidth: isSelected ? 2.5 : 1,
            radius: RADIUS,
            shadowColor: isSelected ? theme.selected : 'rgba(0, 0, 0, 0.2)',
            shadowBlur: isSelected ? 16 : 6,
          },
          name: 'card-body',
          draggable: true,
        })

        // ── Type color bar (left edge) ──
        group.addShape('rect', {
          attrs: {
            x: 0,
            y: 0,
            width: TYPE_BAR_WIDTH,
            height: ASSET_CARD_HEIGHT,
            fill: accent,
            radius: [RADIUS, 0, 0, RADIUS],
          },
          name: 'type-bar',
          draggable: true,
        })

        // ── Asset name (hyperlink affordance) ──
        group.addShape('text', {
          attrs: {
            x: 16,
            y: 26,
            text: truncate(label || '', 26),
            fontSize: 13,
            fontWeight: 600,
            fill: theme.title,
            fontFamily: 'Noto Sans, PingFang SC, sans-serif',
            cursor: 'pointer',
          },
          name: 'asset-title',
          draggable: false,
        })

        // ── Group name / type subtitle ──
        group.addShape('text', {
          attrs: {
            x: 16,
            y: 46,
            text: truncate(groupName || assetType || '', 30),
            fontSize: 11,
            fill: theme.subtitle,
            fontFamily: 'Noto Sans, PingFang SC, sans-serif',
          },
          name: 'asset-subtitle',
          draggable: true,
        })

        // ── Type tag chip (bottom-left) ──
        const tagText = (assetType || 'data').toUpperCase()
        const tagWidth = Math.max(36, tagText.length * 7 + 12)
        group.addShape('rect', {
          attrs: {
            x: 16,
            y: ASSET_CARD_HEIGHT - 22,
            width: tagWidth,
            height: 16,
            fill: 'transparent',
            stroke: accent,
            lineWidth: 1,
            radius: 8,
          },
          name: 'type-chip-bg',
          draggable: true,
        })
        group.addShape('text', {
          attrs: {
            x: 16 + tagWidth / 2,
            y: ASSET_CARD_HEIGHT - 14,
            text: tagText,
            fontSize: 9,
            fontWeight: 600,
            fill: accent,
            textAlign: 'center',
            textBaseline: 'middle',
            fontFamily: 'Noto Sans, sans-serif',
          },
          name: 'type-chip-text',
          draggable: true,
        })

        // ── Run-status dot (top-right) ──
        group.addShape('circle', {
          attrs: {
            x: ASSET_NODE_WIDTH - 20,
            y: 22,
            r: 5,
            fill: statusColor,
            shadowColor: statusColor,
            shadowBlur: (runStatus || 'none').toLowerCase() === 'running' ? 8 : 0,
          },
          name: 'status-dot',
          draggable: true,
        })

        // ── Dependency count chip (bottom-right) ──
        if (depsCount > 0) {
          group.addShape('text', {
            attrs: {
              x: ASSET_NODE_WIDTH - 14,
              y: ASSET_CARD_HEIGHT - 14,
              text: `↑ ${depsCount}`,
              fontSize: 10,
              fill: theme.muted,
              textAlign: 'right',
              textBaseline: 'middle',
              fontFamily: 'Noto Sans, sans-serif',
            },
            name: 'deps-chip',
            draggable: true,
          })
        }

        return keyShape
      },

      /** Update stroke/shadow on hover/select. */
      setState(name?: string, value?: string | boolean, item?: any) {
        if (!item) return
        const shape = item.get('keyShape')
        if (!shape) return
        const model = item.getModel()
        const theme = getNodeTheme()

        const applyDefault = () => {
          shape.attr({
            stroke: model.isSelected ? theme.selected : theme.cardStroke,
            lineWidth: model.isSelected ? 2.5 : 1,
            shadowColor: model.isSelected ? theme.selected : 'rgba(0, 0, 0, 0.2)',
            shadowBlur: model.isSelected ? 16 : 6,
          })
        }

        if (name === 'selected') {
          if (value) {
            shape.attr({
              stroke: theme.selected,
              lineWidth: 2.5,
              shadowColor: theme.selected,
              shadowBlur: 16,
            })
          } else {
            applyDefault()
          }
        } else if (name === 'hover') {
          if (value) {
            shape.attr({
              stroke: theme.hover,
              lineWidth: 2,
              shadowColor: theme.hover,
              shadowBlur: 10,
            })
          } else {
            applyDefault()
          }
        }
      },

      /** Left-center (incoming) and right-center (outgoing) anchors. */
      getAnchorPoints() {
        return [
          [0, 0.5],
          [1, 0.5],
        ]
      },
    },
    'single-node',
  )
}
