/**
 * Ontology view helpers — small pure utilities shared across the detail tabs.
 */
import type { AIContext, AIContextValue } from '@/types/ontology'

/**
 * Normalise an AI-context value into the structured {@link AIContext} shape.
 *
 * The backend allows AI context to be either a plain string (shorthand for
 * `instructions`) or the full object. This collapses both into one shape and
 * returns `null` when there is nothing meaningful to render.
 */
export function normaliseAiContext(value: AIContextValue | null | undefined): AIContext | null {
  if (!value) return null
  if (typeof value === 'string') {
    const text = value.trim()
    return text ? { instructions: text, synonyms: [], examples: [] } : null
  }
  const instructions = value.instructions?.trim() || null
  const synonyms = value.synonyms ?? []
  const examples = value.examples ?? []
  if (!instructions && synonyms.length === 0 && examples.length === 0) return null
  return { instructions, synonyms, examples }
}

/** Classify a metric by its definition style for badge display. */
export type MetricKind = 'derived' | 'mapped' | 'filtered' | 'base'

export function metricKind(metric: {
  derived?: unknown
  maps_to?: string | null
  filter?: string | null
}): MetricKind {
  if (metric.derived) return 'derived'
  if (metric.maps_to) return 'mapped'
  if (metric.filter) return 'filtered'
  return 'base'
}
