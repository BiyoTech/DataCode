/**
 * Knowledge store — manages business-knowledge documents for the active workspace.
 *
 * Documents are stored as files under {workspace}/knowledge/; this store
 * provides list/create/upload/read/delete operations via the backend API.
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { useWorkspaceStore } from './workspace'
import api from '@/utils/api'
import type { KnowledgeDocument, KnowledgeContentResponse } from '@/types/knowledge'

export const useKnowledgeStore = defineStore('knowledge', () => {
  // ─────────── Helpers ───────────

  function _wsPath(): string {
    const ws = useWorkspaceStore().currentWorkspace
    if (!ws) throw new Error('No workspace selected')
    return ws.path
  }

  // ─────────── State ───────────

  const documents = ref<KnowledgeDocument[]>([])
  const isLoading = ref(false)
  const error = ref<string | null>(null)

  // ─────────── Actions ───────────

  /**
   * Fetch knowledge documents list from the backend.
   * @param search - optional title/tag search term
   */
  async function fetchDocuments(search = ''): Promise<void> {
    isLoading.value = true
    error.value = null
    try {
      const { data } = await api.get('/knowledge/documents/list', {
        params: {
          workspace_path: _wsPath(),
          search: search || undefined,
        },
      })
      documents.value = data.documents || []
    } catch (e: any) {
      error.value = e?.response?.data?.detail || e?.message || 'Failed to fetch documents'
      documents.value = []
    } finally {
      isLoading.value = false
    }
  }

  /**
   * Create a new Markdown knowledge document.
   * @returns the created document
   */
  async function createDocument(
    title: string,
    content: string,
    tags?: string,
  ): Promise<KnowledgeDocument> {
    const { data } = await api.post('/knowledge/documents/create', {
      workspace_path: _wsPath(),
      title,
      content,
      tags: tags || undefined,
    })
    // Prepend the new doc to the local list
    documents.value.unshift(data)
    return data
  }

  /**
   * Upload a file as a knowledge document.
   * @returns the created document metadata
   */
  async function uploadFile(file: File): Promise<KnowledgeDocument> {
    const form = new FormData()
    form.append('workspace_path', _wsPath())
    form.append('file', file)

    const { data } = await api.post('/knowledge/documents/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    documents.value.unshift(data)
    return data
  }

  /**
   * Read a document's full content from the backend.
   */
  async function readDocumentContent(
    documentId: string,
  ): Promise<KnowledgeContentResponse> {
    const { data } = await api.get('/knowledge/documents/read', {
      params: {
        workspace_path: _wsPath(),
        document_id: documentId,
      },
    })
    return data
  }

  /**
   * Delete a knowledge document and remove it from the local list.
   */
  async function deleteDocument(documentId: string): Promise<void> {
    await api.post('/knowledge/documents/delete', {
      workspace_path: _wsPath(),
      document_id: documentId,
    })
    documents.value = documents.value.filter((d) => d.id !== documentId)
  }

  /** Reset store state (e.g. on workspace switch). */
  function reset() {
    documents.value = []
    isLoading.value = false
    error.value = null
  }

  return {
    documents,
    isLoading,
    error,
    fetchDocuments,
    createDocument,
    uploadFile,
    readDocumentContent,
    deleteDocument,
    reset,
  }
})
