import { MessagePlugin } from 'tdesign-vue-next'

import { i18n } from '@/i18n'

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
  /** Gateway error identifier, e.g. "forbidden", "no_membership". */
  code?: string
  /** Gateway authorization denial reason, e.g. "missing_permission". */
  reason?: string
  /** Permission code the denied request was evaluated against. */
  requiredPermission?: string
  /** Stable reason code for a rejected remote Git operation. */
  git_error_reason?: string
  /** Host of the Git remote the operation targeted. */
  git_remote_host?: string
  /** Redacted git stderr, kept for support/diagnostics only. */
  git_detail?: string
}

export interface ParsedApiError {
  message: string
  errorCode?: string
  detail?: ApiErrorDetail
}

/**
 * Localized message for an authorization rejection.
 *
 * Authorization failures are the one error class users hit while doing nothing
 * wrong, so they must never surface as "Request failed with status code 403".
 * The gateway answers every denial with `{ code: 'forbidden', reason,
 * requiredPermission }`; sqlmesh-service answers with
 * `{ error_code: 'PERMISSION_DENIED', detail: <permission> }`. Both are mapped
 * to a localized sentence that names the missing capability.
 *
 * Returns an empty string when the error is not authorization-related, so the
 * caller falls back to the generic extraction path.
 */
function localizeAuthorizationError(status: number | undefined, data?: ApiErrorBody): string {
  if (status !== 403 && !data?.reason && data?.error_code !== 'PERMISSION_DENIED') {
    return ''
  }
  const t = i18n.global.t as (key: string, named?: Record<string, unknown>) => string

  // Denials raised by a downstream service (the gateway already allowed the
  // request, so the two layers disagree — still a permission problem).
  if (data?.error_code === 'PERMISSION_DENIED') {
    const permission = typeof data.detail === 'string' ? data.detail : ''
    return permission
      ? t('errors.forbidden.missingPermission', { permission })
      : t('errors.forbidden.default')
  }

  // Non-authorization 403s carry a dedicated code (e.g. a disabled account).
  const codeKeys: Record<string, string> = {
    account_disabled: 'errors.forbidden.accountDisabled',
    no_membership: 'errors.forbidden.noMembership',
    role_builtin: 'errors.forbidden.roleBuiltin',
    owner_role_locked: 'errors.forbidden.ownerRoleLocked',
  }
  if (data?.code && codeKeys[data.code]) {
    return t(codeKeys[data.code])
  }

  const reasonKeys: Record<string, string> = {
    no_active_tenant: 'errors.forbidden.noTenant',
    route_not_allowed: 'errors.forbidden.routeNotAllowed',
    missing_permission: 'errors.forbidden.missingPermission',
    workspace_not_member: 'errors.forbidden.workspaceNotMember',
    workspace_unresolved: 'errors.forbidden.workspaceUnresolved',
    workspace_path_foreign: 'errors.forbidden.workspacePathForeign',
  }
  const key = (data?.reason && reasonKeys[data.reason]) || ''
  const permission = data?.requiredPermission || ''
  if (key) {
    // The permission code is what makes the message actionable ("ask an admin
    // for X"); without it a generic sentence is clearer than a dangling quote.
    if (!permission && key === reasonKeys.missing_permission) {
      return t('errors.forbidden.default')
    }
    return t(key, { permission })
  }

  // A 403 from an endpoint that does not follow the contract: keep whatever the
  // backend said, but never leak the bare axios status text.
  return data?.message || t('errors.forbidden.default')
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
 * Reason codes a rejected remote Git operation can carry, mapped to i18n keys.
 *
 * The backend answers `GIT_REMOTE_ACCESS_DENIED` with a stable
 * `git_error_reason` instead of raw git stderr, so the user reads an
 * actionable sentence in their own language ("the token has no write access to
 * this repository") rather than "The requested URL returned error: 403".
 * Unknown reasons fall through to the backend's English message.
 */
const GIT_REASON_KEYS: Record<string, string> = {
  credential_missing: 'errors.git.credentialMissing',
  invalid_credential: 'errors.git.invalidCredential',
  no_write_access: 'errors.git.noWriteAccess',
  repo_not_found: 'errors.git.repoNotFound',
  non_fast_forward: 'errors.git.nonFastForward',
  branch_not_found: 'errors.git.branchNotFound',
  host_unreachable: 'errors.git.hostUnreachable',
  unknown: 'errors.git.unknown',
}

function localizeGitError(data?: ApiErrorBody): string {
  const reason = data?.git_error_reason
  if (!reason) {
    return ''
  }
  const key = GIT_REASON_KEYS[reason]
  if (!key) {
    return ''
  }
  const t = i18n.global.t as (key: string, named?: Record<string, unknown>) => string
  const message = t(key, { host: data?.git_remote_host || '' })

  // "unknown" is the only reason with no actionable sentence of its own, so the
  // redacted git stderr is the whole diagnosis. Showing it here saves a trip to
  // the log panel for the one case that most needs it.
  if (reason === 'unknown' && data?.git_detail) {
    return `${message} ${data.git_detail}`
  }
  return message
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
    response?: { status?: number; data?: ApiErrorBody }
    message?: string
  }
  const data = err?.response?.data

  // Remote Git rejections carry their own reason code and are the most
  // actionable class of error, so they are localized before anything else.
  const gitMessage = localizeGitError(data)
  if (gitMessage) {
    return {
      message: gitMessage,
      errorCode: data?.error_code,
      detail: data?.git_detail || data?.detail,
    }
  }

  // Authorization failures win over the generic path: they get a localized,
  // actionable sentence naming the missing permission.
  const authorizationMessage = localizeAuthorizationError(err?.response?.status, data)
  if (authorizationMessage) {
    return {
      message: authorizationMessage,
      errorCode: data?.error_code || data?.code,
      detail: data?.detail,
    }
  }

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
    errorCode: data?.error_code || data?.code,
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
