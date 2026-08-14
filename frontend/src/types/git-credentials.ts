/**
 * Git Credentials type definitions.
 *
 * One credential per host: remote operations resolve the credential by the
 * remote URL's host, so two credentials for the same host would be ambiguous.
 * Account linking (OAuth) is reserved for GitHub; every other provider
 * authenticates with a personal access token.
 */

/** Providers the UI offers a preset for. A custom provider is any other name. */
export type KnownGitProvider = 'github' | 'gitlab' | 'bitbucket' | 'gitea'
export type GitProvider = KnownGitProvider | string
export type AuthType = 'oauth' | 'pat'

/** Providers for which account linking is planned. */
export const OAUTH_CAPABLE_PROVIDERS: KnownGitProvider[] = ['github']

export interface GitCredential {
  id: string
  provider: GitProvider
  nickname: string
  auth_type: AuthType
  email: string
  username: string
  server_url: string
  /** Host this credential authenticates against, derived by the backend. */
  host: string
  is_default: boolean
  oauth_account: string
  created_at: string
}

export interface CreateGitCredentialRequest {
  provider: GitProvider
  nickname?: string
  auth_type: AuthType
  token: string
  email?: string
  username?: string
  server_url?: string
}

export interface UpdateGitCredentialRequest {
  nickname?: string
  token?: string
  email?: string
  username?: string
  server_url?: string
}
