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
  /** Which tenant quota was exhausted (QUOTA_EXCEEDED, inlined at top level). */
  quota_resource?: string
  /** Ceiling of the exhausted quota. */
  quota_limit?: number
  /** Live usage at the moment of the rejection. */
  quota_current?: number
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
 * The backend answers `GIT_REMOTE_ACCESS_DENIED` / `GIT_OPERATION_FAILED` with a
 * stable `git_error_reason`, so the user reads an actionable sentence in their
 * own language ("the token has no write access to this repository") rather than
 * "The requested URL returned error: 403".
 *
 * This map is the frontend's copy of `datacode_common.git_reasons`; a shared
 * contract test fails when the two disagree, because a missing key here
 * silently degrades the error to the generic fallback message.
 */
const GIT_REASON_KEYS: Record<string, string> = {
  credential_missing: 'errors.git.credentialMissing',
  invalid_credential: 'errors.git.invalidCredential',
  no_write_access: 'errors.git.noWriteAccess',
  repo_not_found: 'errors.git.repoNotFound',
  non_fast_forward: 'errors.git.nonFastForward',
  branch_not_found: 'errors.git.branchNotFound',
  host_unreachable: 'errors.git.hostUnreachable',
  repo_creation_denied: 'errors.git.repoCreationDenied',
  repo_creation_failed: 'errors.git.repoCreationFailed',
  remote_empty: 'errors.git.remoteEmpty',
  unknown: 'errors.git.unknown',
}

/** Upper bound on the raw git output appended to a message. */
const GIT_DETAIL_MAX_CHARS = 400

/**
 * Collapse the redacted git output into something a toast can carry.
 *
 * Git writes multi-line stderr with repeated `remote:` prefixes; keeping the
 * line structure but trimming the noise is what makes the original wording
 * ("Repository not found.") recognizable to the user.
 */
function formatGitDetail(detail: string): string {
  const cleaned = detail
    .split('\n')
    .map((line) => line.replace(/^remote:\s*/, '').trim())
    .filter((line) => line.length > 0)
    .join(' ')
  return cleaned.length > GIT_DETAIL_MAX_CHARS
    ? `${cleaned.slice(0, GIT_DETAIL_MAX_CHARS)}…`
    : cleaned
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

  // The remote's own wording is appended for EVERY reason, not just "unknown":
  // the localized sentence explains the class of problem, while the raw output
  // names the concrete object ("repository 'https://.../x.git' not found"). The
  // absence of that line is what made these failures unactionable.
  const detail = data?.git_detail ? formatGitDetail(data.git_detail) : ''
  return detail ? `${message} (${detail})` : message
}

/**
 * Resource codes the quota endpoint and the 429 payload share, mapped to i18n
 * keys. The same vocabulary must localize both places: a user rejected with
 * "concurrent_leases quota reached" and then shown a quota panel saying "open
 * working copy" must recognize they are the same thing.
 */
const QUOTA_RESOURCE_KEYS: Record<string, string> = {
  workspace: 'errors.quota.workspace',
  environment: 'errors.quota.environment',
  concurrent_leases: 'errors.quota.concurrent_leases',
  runtime_replicas: 'errors.quota.runtime_replicas',
}

/**
 * Localized message for a quota rejection (QUOTA_EXCEEDED / 429).
 *
 * The backend inlines `quota_resource`, `quota_limit` and `quota_current` at the
 * top level of the error body. Without this path the user sees the backend's
 * English message with the raw resource code in it ("Tenant t1 reached its
 * concurrent_leases quota"); with it they get a localized sentence naming the
 * budget in the platform's own vocabulary and their actual numbers.
 */
function localizeQuotaError(data?: ApiErrorBody): string {
  if (!data?.quota_resource && data?.error_code !== 'QUOTA_EXCEEDED') {
    return ''
  }
  const t = i18n.global.t as (key: string, named?: Record<string, unknown>) => string
  const resourceKey = QUOTA_RESOURCE_KEYS[data?.quota_resource || '']
  const resource = resourceKey ? t(resourceKey) : (data?.quota_resource || '')
  const limit = typeof data?.quota_limit === 'number' ? data.quota_limit : undefined
  const current =
    typeof data?.quota_current === 'number' ? data.quota_current : undefined

  if (resource && limit !== undefined && current !== undefined) {
    return t('errors.quota.exceeded', { resource, current, limit })
  }
  return t('errors.quota.default')
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

  // Quota rejections: the numbers make the sentence actionable and localized,
  // and the 429 means the user did nothing wrong — same reasoning as the
  // authorization path below.
  const quotaMessage = localizeQuotaError(data)
  if (quotaMessage) {
    return {
      message: quotaMessage,
      errorCode: data?.error_code,
      detail: data?.detail,
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
