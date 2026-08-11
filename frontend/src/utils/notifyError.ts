import { MessagePlugin } from 'tdesign-vue-next'

type ApiErrorDetail =
  | string
  | { error?: string; message?: string; detail?: string }
  | Array<{ msg?: string; loc?: string[] }>

/**
 * Shape of the structured error body returned by sqlmesh-service.
 *
 * After the backend error-handling overhaul, every error response — whether
 * from a `ServiceError`, an `HTTPException`, a `RequestValidationError`, or
 * an unhandled `Exception` — shares this same top-level shape so the frontend
 * only needs one parsing path:
 *
 *   {
 *     error_code: string,         // e.g. "DB_ENGINE_ERROR", "QUERY_FAILED"
 *     message: string,            // the actionable root-cause message
 *     detail?: string | object    // optional extra context (cause chain, ...)
 *   }
 *
 * `message` is now the PRIMARY field to display because the backend surfaces
 * the raw exception text there (e.g. "cannot delete from table _versions
 * because it does not have a replica identity"). `detail` is supplementary
 * context (cause chain, validation error list, ...) and should only be shown
 * when `message` is empty.
 */
export interface ApiErrorBody {
  error_code?: string
  message?: string
  detail?: ApiErrorDetail
  error?: string
}

export interface ParsedApiError {
  message: string
  errorCode?: string
  detail?: ApiErrorDetail
}

function stringifyDetail(detail: ApiErrorDetail): string {
  if (typeof detail === 'string') {
    return detail
  }
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join('; ')
  }
  if (detail && typeof detail === 'object') {
    return detail.error || detail.message || detail.detail || JSON.stringify(detail)
  }
  return ''
}

/**
 * Parse an axios/fetch error into a structured `{ message, errorCode, detail }`.
 *
 * Priority: `data.message` (root cause) > `data.detail` (supplementary) >
 * `data.error` > axios `error.message` > `fallback`.
 *
 * This priority is intentional: after the backend unification, `message`
 * carries the actionable root cause (e.g. the raw psycopg2 error text),
 * while `detail` may carry the full cause chain or a validation error list
 * that is too verbose for a toast.
 */
export function extractApiError(error: unknown, fallback = 'Request failed'): ParsedApiError {
  const err = error as {
    response?: { data?: ApiErrorBody }
    message?: string
  }
  const data = err?.response?.data

  let message = ''
  if (data?.message) {
    message = String(data.message)
  } else if (data?.detail) {
    message = stringifyDetail(data.detail)
  } else if (data?.error) {
    message = String(data.error)
  } else if (err?.message) {
    message = err.message
  }
  if (!message) {
    message = fallback
  }

  return {
    message,
    errorCode: data?.error_code,
    detail: data?.detail,
  }
}

/**
 * Backwards-compatible single-string extractor (returns just the message).
 */
export function extractApiMessage(error: unknown, fallback = 'Request failed'): string {
  return extractApiError(error, fallback).message
}

export function notifyError(error: unknown, fallback?: string): void {
  const parsed = extractApiError(error, fallback)
  console.error('[API Error]', parsed.message, error)
  MessagePlugin.error(parsed.message)
}
