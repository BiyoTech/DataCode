package middleware

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/auth"
)

// publicPaths are the only endpoints reachable without an access token.
// Everything else is default-deny.
var publicPaths = map[string]struct{}{
	"/health":                    {},
	"/api/v1/auth/register":      {},
	"/api/v1/auth/login":         {},
	"/api/v1/auth/select-tenant": {},
	"/api/v1/auth/refresh":       {},
}

// JWTAuth validates the bearer access token, publishes the resolved identity
// into the request context and — critically — overwrites the downstream
// identity headers so a client can never forge them.
func JWTAuth(issuer *auth.TokenIssuer, logger *zap.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// Preflight requests carry no credentials.
			if r.Method == http.MethodOptions || isPublicPath(r.URL.Path) {
				next.ServeHTTP(w, r)
				return
			}

			token := bearerToken(r)
			if token == "" {
				writeAuthError(w, http.StatusUnauthorized, "unauthorized",
					"Authentication required.")
				return
			}

			// Only full access tokens are accepted here: a pre-auth token is
			// limited to completing tenant selection.
			claims, err := issuer.Verify(token, auth.TokenTypeAccess)
			if err != nil {
				status, code := http.StatusUnauthorized, "unauthorized"
				message := "Invalid or expired credentials."
				if errors.Is(err, auth.ErrTokenExpired) {
					code = "token_expired"
					message = "Access token expired."
				}
				logger.Debug("auth_middleware_rejected",
					zap.String("path", r.URL.Path), zap.Error(err))
				writeAuthError(w, status, code, message)
				return
			}
			if claims.TenantID == "" {
				writeAuthError(w, http.StatusUnauthorized, "unauthorized",
					"Token is not bound to a tenant identity.")
				return
			}

			ctx := r.Context()
			ctx = context.WithValue(ctx, UserIDKey, claims.Subject)
			ctx = context.WithValue(ctx, TenantIDKey, claims.TenantID)
			ctx = context.WithValue(ctx, UsernameKey, claims.Username)
			ctx = context.WithValue(ctx, RoleKey, claims.Role)

			// Overwrite (never trust) client-supplied identity headers.
			r.Header.Set("X-User-ID", claims.Subject)
			r.Header.Set("X-Tenant-ID", claims.TenantID)
			r.Header.Set("X-Username", claims.Username)
			// Capability headers are produced exclusively by Authorize.
			StripCapabilityHeaders(r)

			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// isPublicPath reports whether a path bypasses authentication. Trailing
// slashes are normalized so "/api/v1/auth/login/" behaves identically.
func isPublicPath(path string) bool {
	normalized := strings.TrimSuffix(path, "/")
	if normalized == "" {
		normalized = "/"
	}
	_, ok := publicPaths[normalized]
	return ok
}

// bearerToken extracts the token from the Authorization header.
func bearerToken(r *http.Request) string {
	header := r.Header.Get("Authorization")
	const prefix = "Bearer "
	if len(header) <= len(prefix) || !strings.EqualFold(header[:len(prefix)], prefix) {
		return ""
	}
	return strings.TrimSpace(header[len(prefix):])
}

func writeAuthError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{
		"code":    code,
		"message": message,
	})
}
