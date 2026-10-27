/**
 * File store - manages file tree state, open files, and file content.
 * Provides file operations backed by the Gateway API.
 *
 * Also owns the editor draft autosave layer (design 07 Q6): keystrokes are
 * debounced for 5 seconds into `file_drafts` in the database, so typed-but-
 * unsaved work survives even the loss of a replica's disk. The draft is
 * deleted on save and restored (as a dirty buffer) when a file is opened.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import api from '@/utils/api'
import { i18n } from '@/i18n'
import { useWorkspaceStore } from '@/stores/workspace'
import type { DepsDeclarationDiagnostics } from '@/types/dagster'

export interface FileNode {
  path: string          // Relative to workspace root
  name: string          // Filename only
  isDirectory: boolean
  size: number
  children?: FileNode[] // Populated on expand (lazy loading)
  isExpanded?: boolean
  isLoading?: boolean
}

export interface OpenFile {
  path: string
  name: string
  content: string
  language: string
  isDirty: boolean      // Has unsaved changes
  originalContent: string
  readOnly?: boolean    // Virtual/read-only content such as Git diffs
  isDiff?: boolean      // Render as a side-by-side diff (before vs after)
  diffOriginal?: string // "Before" content used as the left side of the diff
}

export const useFileStore = defineStore('files', () => {
  const workspaceStore = useWorkspaceStore()

  // File tree state
  const fileTree = ref<FileNode[]>([])
  const isLoadingTree = ref(false)

  // Flat file list for @-mention file picker (cached per workspace)
  const flatFiles = ref<{ path: string; name: string }[]>([])
  const isLoadingFlat = ref(false)
  let flatFilesWorkspacePath = ''

  // Open files / tabs
  const openFiles = ref<OpenFile[]>([])
  const activeFilePath = ref<string | null>(null)

  const activeFile = computed(() =>
    openFiles.value.find((f) => f.path === activeFilePath.value) || null
  )

  /**
   * Platform verdict on a saved dependency declaration, keyed by file path.
   *
   * Only `asset-dag/pyproject.toml` ever gets an entry. It exists so a version
   * bound the platform cannot satisfy is visible the moment it is saved — the
   * check is a parse plus a comparison, while discovering the same thing through
   * a reload costs a full install cycle.
   */
  const depsDiagnostics = ref<Record<string, DepsDeclarationDiagnostics>>({})

  /** Verdict for the file currently in the editor, if it has one. */
  const activeDepsDiagnostics = computed(() =>
    activeFilePath.value ? depsDiagnostics.value[activeFilePath.value] || null : null
  )

  /**
   * Load file tree for a workspace (root level).
   */
  async function loadFileTree(workspacePath: string): Promise<void> {
    isLoadingTree.value = true
    try {
      const { data } = await api.get('/files/list', {
        params: { workspace_path: workspacePath, directory: '', recursive: false },
      })
      fileTree.value = (data.files || []).map((f: any) => fileNodeFromApi(f))
    } finally {
      isLoadingTree.value = false
    }
  }

  /**
   * Silently refresh the root file tree, preserving expanded state of
   * existing directories. Used for auto-polling without disturbing UX.
   */
  async function refreshFileTree(workspacePath: string): Promise<void> {
    try {
      const { data } = await api.get('/files/list', {
        params: { workspace_path: workspacePath, directory: '', recursive: false },
      })
      const freshNodes: FileNode[] = (data.files || []).map((f: any) => fileNodeFromApi(f))

      // Build lookup of existing nodes by path to preserve expanded state
      const existingMap = new Map<string, FileNode>()
      for (const node of fileTree.value) {
        existingMap.set(node.path, node)
      }

      // Merge: carry over isExpanded, children, isLoading for directories that still exist
      const merged: FileNode[] = freshNodes.map((fresh) => {
        const existing = existingMap.get(fresh.path)
        if (existing && existing.isDirectory && fresh.isDirectory) {
          fresh.isExpanded = existing.isExpanded
          fresh.isLoading = existing.isLoading
          fresh.children = existing.children
        }
        return fresh
      })

      fileTree.value = merged
    } catch {
      // Silently ignore transient errors during auto-refresh
    }
  }

  /**
   * Fetch a flat list of all files in the workspace (recursive).
   * Results are cached per workspace — subsequent calls return instantly.
   */
  async function fetchFlatFiles(workspacePath: string): Promise<{ path: string; name: string }[]> {
    if (flatFilesWorkspacePath === workspacePath && flatFiles.value.length > 0) {
      return flatFiles.value
    }
    isLoadingFlat.value = true
    try {
      const { data } = await api.get('/files/list', {
        params: { workspace_path: workspacePath, directory: '', recursive: true },
      })
      flatFiles.value = (data.files || [])
        .filter((f: any) => !f.is_directory)
        .map((f: any) => ({
          path: f.path,
          name: f.path.split('/').pop() || f.path,
        }))
      flatFilesWorkspacePath = workspacePath
      return flatFiles.value
    } finally {
      isLoadingFlat.value = false
    }
  }

  /**
   * Expand a directory node (lazy load children).
   */
  async function expandDirectory(workspacePath: string, node: FileNode): Promise<void> {
    if (!node.isDirectory) return

    node.isLoading = true
    try {
      const { data } = await api.get('/files/list', {
        params: { workspace_path: workspacePath, directory: node.path, recursive: false },
      })
      node.children = (data.files || []).map((f: any) => fileNodeFromApi(f))
      node.isExpanded = true
    } finally {
      node.isLoading = false
    }
  }

  /**
   * Collapse a directory node.
   */
  function collapseDirectory(node: FileNode) {
    node.isExpanded = false
  }

  /**
   * Toggle directory expand/collapse.
   */
  async function toggleDirectory(workspacePath: string, node: FileNode) {
    if (node.isExpanded) {
      collapseDirectory(node)
    } else {
      await expandDirectory(workspacePath, node)
    }
  }

  /**
   * Open a file (load content and add to tabs).
   */
  async function openFile(workspacePath: string, filePath: string): Promise<void> {
    // Check if already open
    const existing = openFiles.value.find((f) => f.path === filePath)
    if (existing) {
      activeFilePath.value = filePath
      return
    }

    const { data } = await api.get('/files/read', {
      params: { workspace_path: workspacePath, file_path: filePath },
    })

    const name = filePath.split('/').pop() || filePath
    const language = detectLanguage(name)

    const file: OpenFile = {
      path: filePath,
      name,
      content: data.content,
      language,
      isDirty: false,
      originalContent: data.content,
    }
    openFiles.value.push(file)
    activeFilePath.value = filePath

    await restoreDraftIfNewer(workspacePath, file)
  }

  /**
   * Save the active file to backend.
   *
   * Saving `asset-dag/pyproject.toml` additionally surfaces the platform's
   * verdict on the declaration (see `depsDiagnostics`). The save itself is never
   * blocked by it: the file is the user's, an intermediate edit is legitimately
   * broken, and a write that refused itself would make the editor unusable.
   */
  async function saveFile(workspacePath: string, filePath: string): Promise<void> {
    const file = openFiles.value.find((f) => f.path === filePath)
    if (!file || file.readOnly) return

    const { data } = await api.post<{
      success: boolean
      deps?: DepsDeclarationDiagnostics
    }>('/files/write', {
      workspace_path: workspacePath,
      file_path: filePath,
      content: file.content,
    })

    file.isDirty = false
    file.originalContent = file.content

    // Kept per path rather than as a single value: several files can be open,
    // and a stale verdict attached to the wrong tab is worse than none.
    if (data.deps) {
      depsDiagnostics.value = { ...depsDiagnostics.value, [filePath]: data.deps }
    } else if (depsDiagnostics.value[filePath]) {
      const next = { ...depsDiagnostics.value }
      delete next[filePath]
      depsDiagnostics.value = next
    }

    // The draft has served its purpose; keeping it would make the next open
    // offer keystrokes that were already saved.
    discardDraft(workspacePath, filePath)
  }

  /**
   * Open or replace an in-memory read-only tab, such as a Git diff.
   */
  function openVirtualFile(file: Omit<OpenFile, 'isDirty' | 'originalContent'>) {
    const existing = openFiles.value.find((f) => f.path === file.path)
    if (existing) {
      existing.name = file.name
      existing.content = file.content
      existing.language = file.language
      existing.readOnly = file.readOnly
      existing.isDirty = false
      existing.originalContent = file.content
    } else {
      openFiles.value.push({
        ...file,
        isDirty: false,
        originalContent: file.content,
      })
    }
    activeFilePath.value = file.path
  }

  /**
   * Open a read-only side-by-side diff tab (before vs after).
   * Used by file-change cards to inspect what a patch modified.
   */
  function openDiffFile(
    filePath: string,
    oldContent: string,
    newContent: string,
    language: string,
  ) {
    // Use a synthetic path so the diff tab never collides with the editable file tab.
    const tabPath = `diff://${filePath}`
    const name = `${filePath.split('/').pop() || filePath} (diff)`

    const existing = openFiles.value.find((f) => f.path === tabPath)
    if (existing) {
      existing.name = name
      existing.content = newContent
      existing.diffOriginal = oldContent
      existing.language = language
      existing.originalContent = newContent
    } else {
      openFiles.value.push({
        path: tabPath,
        name,
        content: newContent,
        language,
        isDirty: false,
        originalContent: newContent,
        readOnly: true,
        isDiff: true,
        diffOriginal: oldContent,
      })
    }
    activeFilePath.value = tabPath
  }

  /**
   * Update file content in memory (mark dirty).
   */
  function updateFileContent(filePath: string, content: string) {
    const file = openFiles.value.find((f) => f.path === filePath)
    if (!file || file.readOnly) return
    file.content = content
    file.isDirty = content !== file.originalContent
    // Durability contract <=5 s layer: schedule the draft flush.
    draftDirty.add(filePath)
    scheduleDraftFlush()
  }

  /**
   * Close a file tab.
   */
  function closeFile(filePath: string) {
    const idx = openFiles.value.findIndex((f) => f.path === filePath)
    if (idx === -1) return

    // The debounce timer might not have fired yet; flush the draft now or the
    // last keystrokes are lost with the tab.
    const wsPath = workspaceStore.currentWorkspace?.path
    const file = openFiles.value[idx]
    if (wsPath && draftDirty.has(filePath)) {
      draftDirty.delete(filePath)
      void api
        .put('/files/draft', {
          workspace_path: wsPath,
          file_path: filePath,
          content: file.content,
        })
        .catch(() => {
          /* best-effort: the durability contract is 5 s, not zero */
        })
    } else {
      draftDirty.delete(filePath)
    }

    openFiles.value.splice(idx, 1)

    // Update active file
    if (activeFilePath.value === filePath) {
      activeFilePath.value = openFiles.value[idx]?.path
        || openFiles.value[idx - 1]?.path
        || null
    }
  }

  /**
   * Create a new file.
   */
  async function createFile(workspacePath: string, filePath: string, content = ''): Promise<void> {
    await api.post('/files/write', {
      workspace_path: workspacePath,
      file_path: filePath,
      content,
    })
  }

  /**
   * Upload one file into a directory of the working copy.
   *
   * Multipart rather than the JSON `/write` because uploads can be binary
   * (images, archives, spreadsheets). Overwrites an existing file of the same
   * name — the same contract as saving in the editor.
   *
   * `workspace_path` rides the query string, not the form: the gateway reads it
   * to authorize against the right workspace and to route to the replica holding
   * this copy's lease, and it cannot inspect a multipart body for either.
   */
  async function uploadFile(
    workspacePath: string,
    directory: string,
    file: File,
  ): Promise<{ path: string; size: number }> {
    const form = new FormData()
    form.append('directory', directory)
    form.append('file', file)

    const { data } = await api.post('/files/upload', form, {
      params: { workspace_path: workspacePath },
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return data
  }

  /**
   * Create a new directory.
   */
  async function createDirectory(workspacePath: string, dirPath: string): Promise<void> {
    await api.post('/files/mkdir', {
      workspace_path: workspacePath,
      directory: dirPath,
    })
  }

  /**
   * Delete a file or directory.
   *
   * The backend has one delete semantic: a directory goes with everything under
   * it, and neither case can be undone. The confirmation therefore lives in the
   * caller (the explorer's dialog) — by the time this runs, the user has agreed.
   */
  async function deleteFile(workspacePath: string, filePath: string): Promise<void> {
    await api.post('/files/delete', {
      workspace_path: workspacePath,
      file_path: filePath,
    })
    // Close the deleted path's tab and every tab that lived inside it. The
    // trailing slash matters: "models" must not close "models_old/x.sql".
    const prefix = `${filePath}/`
    for (const file of [...openFiles.value]) {
      if (file.path === filePath || file.path.startsWith(prefix)) {
        closeFile(file.path)
      }
    }
  }

  /**
   * Rename a file or directory.
   */
  async function renameFile(workspacePath: string, oldPath: string, newPath: string): Promise<void> {
    await api.post('/files/rename', {
      workspace_path: workspacePath,
      old_path: oldPath,
      new_path: newPath,
    })

    // Update open file tab if renamed
    const file = openFiles.value.find((f) => f.path === oldPath)
    if (file) {
      file.path = newPath
      file.name = newPath.split('/').pop() || newPath
      if (activeFilePath.value === oldPath) {
        activeFilePath.value = newPath
      }
    }
  }

  // ── Editor draft autosave (durability contract <=5 s layer, design 07 Q6) ──
  //
  // Debounced rather than on-every-keystroke: a draft write is a DB round
  // trip, and 5 s is exactly the window the contract publishes.

  const DRAFT_DEBOUNCE_MS = 5000
  const draftDirty = new Set<string>()
  let draftTimer: ReturnType<typeof setTimeout> | null = null

  function scheduleDraftFlush(): void {
    if (draftTimer !== null) clearTimeout(draftTimer)
    draftTimer = setTimeout(() => {
      draftTimer = null
      void flushDrafts()
    }, DRAFT_DEBOUNCE_MS)
  }

  /** Persist every dirty draft; a failure keeps it dirty for the next edit. */
  async function flushDrafts(): Promise<void> {
    const wsPath = workspaceStore.currentWorkspace?.path
    if (!wsPath || draftDirty.size === 0) return
    for (const filePath of [...draftDirty]) {
      const file = openFiles.value.find((f) => f.path === filePath)
      if (!file) {
        draftDirty.delete(filePath)
        continue
      }
      const snapshot = file.content
      try {
        await api.put('/files/draft', {
          workspace_path: wsPath,
          file_path: filePath,
          content: snapshot,
        })
        // Only clear when nothing was typed mid-flight; otherwise the next
        // keystroke's schedule covers it.
        if (file.content === snapshot) draftDirty.delete(filePath)
      } catch {
        /* silent: the draft layer is best-effort within its 5 s bound */
      }
    }
  }

  /** Delete the draft after a save (or after the user discards a recovery). */
  function discardDraft(workspacePath: string, filePath: string): void {
    draftDirty.delete(filePath)
    void api
      .post('/files/draft/delete', {
        workspace_path: workspacePath,
        file_path: filePath,
      })
      .catch(() => {
        /* a stale draft resurfaces as an already-saved buffer; harmless */
      })
  }

  /**
   * Offer recovery when a newer draft exists (crash restore / the seam with
   * the WIP snapshot layer: a snapshot can be up to 10 min old, a draft at
   * most 5 s). The draft replaces the buffer and marks it dirty, so the user
   * stays in control: save to keep it, close the tab to drop it.
   */
  async function restoreDraftIfNewer(
    workspacePath: string,
    file: OpenFile,
  ): Promise<void> {
    try {
      const { data } = await api.get('/files/draft', {
        params: { workspace_path: workspacePath, file_path: file.path },
      })
      if (typeof data?.content !== 'string') return
      if (data.content === file.content) return
      file.content = data.content
      file.isDirty = true
      const t = i18n.global.t as (key: string, named?: Record<string, unknown>) => string
      MessagePlugin.info(t('editor.draftRestored', { file: file.name }))
    } catch {
      /* 404 = no draft: the normal case after every clean save */
    }
  }

  /**
   * Reset store state (on workspace switch).
   */
  function reset() {
    if (draftTimer !== null) {
      clearTimeout(draftTimer)
      draftTimer = null
    }
    draftDirty.clear()
    fileTree.value = []
    flatFiles.value = []
    flatFilesWorkspacePath = ''
    openFiles.value = []
    activeFilePath.value = null
    // Verdicts describe ONE working copy's declaration against the platform.
    // Carrying them across a switch would label the new copy's file with the
    // previous copy's conflicts.
    depsDiagnostics.value = {}
  }

  return {
    fileTree,
    isLoadingTree,
    flatFiles,
    isLoadingFlat,
    openFiles,
    activeFilePath,
    activeFile,
    depsDiagnostics,
    activeDepsDiagnostics,
    loadFileTree,
    refreshFileTree,
    fetchFlatFiles,
    expandDirectory,
    collapseDirectory,
    toggleDirectory,
    openFile,
    saveFile,
    openVirtualFile,
    openDiffFile,
    updateFileContent,
    closeFile,
    createFile,
    uploadFile,
    createDirectory,
    deleteFile,
    renameFile,
    reset,
  }
})

// --- Helpers ---

function fileNodeFromApi(f: { path: string; is_directory: boolean; size: number }): FileNode {
  const name = f.path.split('/').pop() || f.path
  return {
    path: f.path,
    name,
    isDirectory: f.is_directory,
    size: f.size || 0,
    children: f.is_directory ? [] : undefined,
    isExpanded: false,
    isLoading: false,
  }
}

function detectLanguage(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase()
  const langMap: Record<string, string> = {
    sql: 'sql',
    py: 'python',
    yaml: 'yaml',
    yml: 'yaml',
    json: 'json',
    md: 'markdown',
    ts: 'typescript',
    js: 'javascript',
    csv: 'csv',
    txt: 'plaintext',
  }
  return langMap[ext || ''] || 'plaintext'
}
