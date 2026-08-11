/**
 * Type definitions for Knowledge document management.
 * Mirrors the backend KnowledgeDocumentResponse / KnowledgeContentResponse models.
 */

/** Metadata for a single knowledge document. Content is fetched separately. */
export interface KnowledgeDocument {
  id: string
  workspace_path: string
  title: string
  filename: string
  content_type: string // 'markdown' | 'text' | 'pdf' | 'csv' | 'json' | 'yaml'
  file_size: number
  tags: string | null
  created_at: string // UTC ISO 8601
  updated_at: string // UTC ISO 8601
}

/** Paginated document list. */
export interface KnowledgeListResponse {
  documents: KnowledgeDocument[]
  total: number
}

/** Full document content read from disk. */
export interface KnowledgeContentResponse {
  id: string
  title: string
  content_type: string
  content: string
  size: number
}
