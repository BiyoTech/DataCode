/**
 * Deps store — the platform baseline and the one-click conflict fix.
 *
 * Two things the rest of the UI needs and cannot derive:
 *
 * - **the baseline**: what a code location can import with no overlay at all. A
 *   property of the runtime image, so it is fetched once per session and shared
 *   by the dependency panel, the conflict card and the editor diagnostic. A user
 *   cannot write a compatible declaration against a list nobody can read;
 * - **applying a suggested rewrite**: the backend reports each conflict with the
 *   exact requirement string to use instead, and this turns that into an edit of
 *   `asset-dag/pyproject.toml`.
 *
 * The rewrite is deliberately TEXTUAL — line-level, in the file as the user
 * wrote it. Reformatting the TOML would be the alternative, and it would discard
 * their comments, ordering and grouping to fix one version bound.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import api from '@/utils/api'
import { notifyError } from '@/utils/notifyError'
import type { DepsBaseline, DepsConflict } from '@/types/dagster'

/** Where the declaration lives inside a working copy. Fixed by the platform. */
export const DECLARATION_PATH = 'asset-dag/pyproject.toml'

/**
 * Whether a declaration line declares the named distribution.
 *
 * Matched by NAME rather than by the reported requirement string: the backend
 * normalises a requirement before reporting it (specifiers are sorted, so
 * `pyarrow>=14,<15` comes back as `pyarrow<15,>=14`), while the file holds what
 * the user typed. Comparing the two literally would silently fail to match on
 * exactly the multi-bound requirements that conflict most often.
 *
 * A line is a quoted requirement inside the dependencies array, so the name is
 * the leading identifier after the quote — anchored on both sides to keep
 * `pyarrow` from matching `pyarrow-hotfix`.
 */
function declaresPackage(line: string, name: string): boolean {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  // The name may be written with either separator and in any case; PEP 503
  // canonicalisation treats `-`, `_` and `.` as equivalent.
  const flexible = escaped.replace(/-/g, '[-_.]')
  return new RegExp(`^\\s*["']${flexible}\\s*(\\[|[<>=!~;]|["'])`, 'i').test(line)
}

export const useDepsStore = defineStore('deps', () => {
  const baseline = ref<DepsBaseline | null>(null)
  const loadingBaseline = ref(false)

  /** Version the platform ships for a distribution, or `''`. */
  const versionOf = computed(() => (name: string): string => {
    const hit = baseline.value?.packages.find((item) => item.name === name)
    return hit?.version || ''
  })

  /**
   * Fetch the baseline once per session.
   *
   * Cached because it cannot change while the runtime runs — the image is
   * immutable and nothing installs into it — and the panel asks on every open.
   */
  async function loadBaseline(force = false): Promise<DepsBaseline | null> {
    if (baseline.value && !force) return baseline.value
    if (loadingBaseline.value) return baseline.value
    loadingBaseline.value = true
    try {
      const { data } = await api.get<DepsBaseline>('/deps/baseline')
      baseline.value = data
      return data
    } catch (error) {
      // Not fatal: every surface that shows the baseline is informational, and
      // the install path enforces the same rules without it.
      notifyError(error)
      return null
    } finally {
      loadingBaseline.value = false
    }
  }

  /**
   * Rewrite the named conflicts in the working copy's declaration.
   *
   * Line-level and textual on purpose. Re-emitting the TOML from a parsed model
   * would be the alternative, and it would discard the user's comments,
   * ordering and grouping in order to fix one version bound.
   *
   * Takes any {name, suggested} pair, so the same action serves a direct
   * platform conflict and a transitive one: both are one line in this file,
   * and the rewrite was computed by the backend in both cases.
   *
   * Returns the number of lines changed. Zero means no line declared any of
   * these packages any more — someone else edited the file — which must not be
   * reported as a successful fix.
   */
  async function applySuggestions(
    workspacePath: string,
    conflicts: Array<Pick<DepsConflict, 'name' | 'suggested'>>,
  ): Promise<number> {
    const { data } = await api.get<{ content: string }>('/files/read', {
      params: { workspace_path: workspacePath, file_path: DECLARATION_PATH },
    })

    const lines = (data.content ?? '').split('\n')
    let changed = 0
    for (const conflict of conflicts) {
      const index = lines.findIndex((line) => declaresPackage(line, conflict.name))
      if (index === -1) continue
      const line = lines[index]
      const quote = line.includes('"') ? '"' : "'"
      // Indentation and any trailing comma/comment are preserved by rebuilding
      // only the quoted requirement itself.
      const before = line.slice(0, line.indexOf(quote))
      const after = line.slice(line.lastIndexOf(quote) + 1)
      lines[index] = `${before}${quote}${conflict.suggested}${quote}${after}`
      changed += 1
    }

    if (changed === 0) return 0

    await api.post('/files/write', {
      workspace_path: workspacePath,
      file_path: DECLARATION_PATH,
      content: lines.join('\n'),
    })
    return changed
  }

  return {
    baseline,
    loadingBaseline,
    versionOf,
    loadBaseline,
    applySuggestions,
  }
})
