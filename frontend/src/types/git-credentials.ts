/**
 * Git Credentials type definitions.
 * Supports multiple credentials per user: OAuth (one per provider) + PAT (multiple).
 */

export type GitProvider = 'github' | 'gitlab' | 'bitbucket' | 'gitea'
export type AuthType = 'oauth' | 'pat'

export interface GitCredential {
  id: string
  provider: GitProvider
  nickname: string
  auth_type: AuthType
  email: string
  username: string
  server_url: string
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
