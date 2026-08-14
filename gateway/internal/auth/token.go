package auth

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
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
	Username  string `json:"username"`
	TenantID  string `json:"tenant_id,omitempty"`
	Role      string `json:"role,omitempty"`
	TokenType string `json:"typ"`
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

// NewRefreshToken returns a cryptographically random opaque token. Only its
// hash is persisted, so the raw value exists solely in the client's storage.
func NewRefreshToken() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("generate refresh token: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(buf), nil
}

// HashRefreshToken derives the lookup hash for a raw refresh token.
func HashRefreshToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

// newID returns a random identifier for database primary keys.
func newID() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		// crypto/rand failure is unrecoverable for identity issuance.
		panic(fmt.Sprintf("crypto/rand unavailable: %v", err))
	}
	return hex.EncodeToString(buf)
}

// formatUTC renders a timestamp for storage (always UTC, RFC3339 nanos).
func formatUTC(t time.Time) string {
	return t.UTC().Format(time.RFC3339Nano)
}

// parseUTC reads a stored timestamp back as UTC; unparsable values yield the
// zero time so a corrupt row cannot crash a request.
func parseUTC(value string) time.Time {
	if value == "" {
		return time.Time{}
	}
	t, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		return time.Time{}
	}
	return t.UTC()
}

// isUniqueViolation reports whether err is a UNIQUE constraint violation.
// It handles both SQLite and MySQL error formats:
//   - SQLite: "UNIQUE constraint failed: ..."
//   - MySQL:  "Error 1062: Duplicate entry '...' for key '...'"
func isUniqueViolation(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "UNIQUE constraint failed") ||
		strings.Contains(msg, "Error 1062") ||
		strings.Contains(msg, "Duplicate entry")
}
