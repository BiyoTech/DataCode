// Package auth holds the gateway's TOKEN VERIFICATION primitives.
//
// P5.5 scope change: this package no longer authenticates anyone. Login,
// registration, refresh rotation, password changes and the identity tables all
// moved to identity-service (design 06 P2); the gateway proxies those endpoints
// and keeps only what it needs to enforce identity on EVERY request — verifying
// the access token that identity-service minted.
//
// The Claims struct must therefore stay wire-compatible with
// identity-service's: both sign and verify with the same GW_JWT_SECRET, and a
// field the gateway cannot read is a field it cannot enforce.
package auth

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// Token types carried in the JWT "typ" claim. A pre-auth token only allows
// completing the identity selection step and must never reach business APIs.
const (
	TokenTypeAccess  = "access"
	TokenTypePreAuth = "pre_auth"
)

// Errors surfaced by token verification.
var (
	ErrInvalidToken   = errors.New("invalid token")
	ErrTokenExpired   = errors.New("token expired")
	ErrWrongTokenType = errors.New("unexpected token type")
	ErrMissingSecret  = errors.New("jwt secret is not configured")
)

// Claims is the JWT payload. For pre-auth tokens TenantID and Role are empty
// because the tenant identity has not been chosen yet.
type Claims struct {
	Username string `json:"username"`
	TenantID string `json:"tenant_id,omitempty"`
	Role     string `json:"role,omitempty"`
	// CellID is the cell serving this tenant. Minted by identity-service; the
	// gateway only reads it (the global edge routes on it).
	CellID string `json:"cell_id,omitempty"`
	// PermsVersion is the tenant's authorization generation at issue time.
	// The authz resolver compares it against its cached snapshot so a
	// revocation takes effect on the next request (design 03 §2.5).
	PermsVersion int64  `json:"perms_version,omitempty"`
	TokenType    string `json:"typ"`
	jwt.RegisteredClaims
}

// TokenIssuer signs and verifies gateway JWTs with a symmetric secret.
type TokenIssuer struct {
	secret     []byte
	accessTTL  time.Duration
	preAuthTTL time.Duration
	refreshTTL time.Duration
}

// NewTokenIssuer builds an issuer. The secret must come from the environment.
func NewTokenIssuer(secret string, accessTTL, preAuthTTL, refreshTTL time.Duration) (*TokenIssuer, error) {
	if strings.TrimSpace(secret) == "" {
		return nil, ErrMissingSecret
	}
	return &TokenIssuer{
		secret:     []byte(secret),
		accessTTL:  accessTTL,
		preAuthTTL: preAuthTTL,
		refreshTTL: refreshTTL,
	}, nil
}

// RefreshTTL exposes the configured refresh token lifetime.
func (ti *TokenIssuer) RefreshTTL() time.Duration { return ti.refreshTTL }

// IssueAccessToken signs an access token bound to a tenant identity and
// returns the token plus its UTC expiry.
func (ti *TokenIssuer) IssueAccessToken(userID, username, tenantID, role string) (string, time.Time, error) {
	expiresAt := time.Now().UTC().Add(ti.accessTTL)
	token, err := ti.sign(Claims{
		Username:  username,
		TenantID:  tenantID,
		Role:      role,
		TokenType: TokenTypeAccess,
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   userID,
			ExpiresAt: jwt.NewNumericDate(expiresAt),
			IssuedAt:  jwt.NewNumericDate(time.Now().UTC()),
		},
	})
	return token, expiresAt, err
}

// IssuePreAuthToken signs the short-lived token used to complete identity
// selection when a user belongs to multiple tenants.
func (ti *TokenIssuer) IssuePreAuthToken(userID, username string) (string, time.Time, error) {
	expiresAt := time.Now().UTC().Add(ti.preAuthTTL)
	token, err := ti.sign(Claims{
		Username:  username,
		TokenType: TokenTypePreAuth,
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   userID,
			ExpiresAt: jwt.NewNumericDate(expiresAt),
			IssuedAt:  jwt.NewNumericDate(time.Now().UTC()),
		},
	})
	return token, expiresAt, err
}

func (ti *TokenIssuer) sign(claims Claims) (string, error) {
	signed, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString(ti.secret)
	if err != nil {
		return "", fmt.Errorf("sign token: %w", err)
	}
	return signed, nil
}

// Verify parses and validates a token, additionally asserting its type.
func (ti *TokenIssuer) Verify(tokenString, expectedType string) (*Claims, error) {
	claims := &Claims{}
	_, err := jwt.ParseWithClaims(tokenString, claims, func(t *jwt.Token) (interface{}, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, ErrInvalidToken
		}
		return ti.secret, nil
	}, jwt.WithValidMethods([]string{jwt.SigningMethodHS256.Alg()}))
	if err != nil {
		if errors.Is(err, jwt.ErrTokenExpired) {
			return nil, ErrTokenExpired
		}
		return nil, ErrInvalidToken
	}
	if claims.TokenType != expectedType {
		return nil, ErrWrongTokenType
	}
	if claims.Subject == "" {
		return nil, ErrInvalidToken
	}
	return claims, nil
}

// ---------------------------------------------------------------------------
// Refresh token primitives
// ---------------------------------------------------------------------------
//
// P5.5: minting and hashing refresh tokens moved to identity-service together
// with the `refresh_tokens` table. The gateway never sees a refresh token any
// more — it proxies /auth/refresh — so those helpers were removed rather than
// left as an unused second implementation.

