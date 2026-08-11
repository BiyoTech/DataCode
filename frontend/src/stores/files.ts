/**
 * File store - manages file tree state, open files, and file content.
 * Provides file operations backed by the Gateway API.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/utils/api'

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

    openFiles.value.push({
      path: filePath,
      name,
      content: data.content,
      language,
      isDirty: false,
      originalContent: data.content,
    })

    activeFilePath.value = filePath
  }

  /**
   * Save the active file to backend.
   */
  async function saveFile(workspacePath: string, filePath: string): Promise<void> {
    const file = openFiles.value.find((f) => f.path === filePath)
    if (!file || file.readOnly) return

    await api.post('/files/write', {
      workspace_path: workspacePath,
      file_path: filePath,
      content: file.content,
    })

    file.isDirty = false
    file.originalContent = file.content
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
  }

  /**
   * Close a file tab.
   */
  function closeFile(filePath: string) {
    const idx = openFiles.value.findIndex((f) => f.path === filePath)
    if (idx === -1) return

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
   */
  async function deleteFile(workspacePath: string, filePath: string): Promise<void> {
    await api.post('/files/delete', {
      workspace_path: workspacePath,
      file_path: filePath,
    })
    // Close tab if open
    closeFile(filePath)
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

  /**
   * Reset store state (on workspace switch).
   */
  function reset() {
    fileTree.value = []
    flatFiles.value = []
    flatFilesWorkspacePath = ''
    openFiles.value = []
    activeFilePath.value = null
  }

  return {
    fileTree,
    isLoadingTree,
    flatFiles,
    isLoadingFlat,
    openFiles,
    activeFilePath,
    activeFile,
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
