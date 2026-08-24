package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/auth"
	"github.com/datacode/gateway/internal/config"
	"github.com/datacode/gateway/internal/rbac"
	"github.com/datacode/gateway/internal/router"
)

func main() {
	logger, _ := zap.NewProduction()
	defer logger.Sync()
	// Expose the logger globally so infrastructure code that runs before any
	// service is wired (e.g. schema migration inside the store constructor)
	// can still report what it does.
	zap.ReplaceGlobals(logger)

	cfg := config.Load()

	// Authentication is mandatory: without a signing secret the gateway
	// cannot issue or verify identities, so fail fast instead of starting in
	// an insecure state.
	issuer, err := auth.NewTokenIssuer(
		config.JWTSecret(),
		time.Duration(cfg.Auth.AccessTokenTTLMinutes)*time.Minute,
		time.Duration(cfg.Auth.PreAuthTokenTTLMinutes)*time.Minute,
		time.Duration(cfg.Auth.RefreshTokenTTLHours)*time.Hour,
	)
	if err != nil {
		logger.Fatal("auth token issuer initialization failed",
			zap.Error(err),
			zap.String("hint", "set the GW_JWT_SECRET environment variable"))
	}

	store, err := auth.NewStore(auth.Dialect(cfg.Auth.AuthDialect), cfg.Auth.AuthDSN)
	if err != nil {
		logger.Fatal("auth store initialization failed", zap.Error(err))
	}
	defer store.Close()
	logger.Info("auth store ready",
		zap.String("dialect", cfg.Auth.AuthDialect),
		zap.String("dsn", logSafeDSN(cfg.Auth.AuthDSN)))

	limiter := auth.NewRateLimiter(
		cfg.Auth.LoginMaxAttempts,
		time.Duration(cfg.Auth.LoginLockoutMinutes)*time.Minute,
	)
	authService := auth.NewService(store, issuer, limiter, cfg.Auth.BcryptCost, logger)

	// Authorization data must exist before the first request is served:
	// the permission catalog is synchronized from code and the system role
	// templates are seeded. A failure here means no decision can be trusted.
	rbacService := rbac.NewService(rbac.NewStore(store.DB()), logger)
	if err := rbacService.Bootstrap(context.Background()); err != nil {
		logger.Fatal("rbac bootstrap failed", zap.Error(err))
	}

	// Purge tokens that can no longer be used; failure is non-fatal.
	if purged, err := store.DeleteExpiredRefreshTokens(context.Background()); err != nil {
		logger.Warn("expired refresh token cleanup failed", zap.Error(err))
	} else if purged > 0 {
		logger.Info("expired refresh tokens purged", zap.Int64("count", purged))
	}

	r := router.Setup(cfg, logger, authService, rbacService)

	srv := &http.Server{
		Addr:         fmt.Sprintf(":%d", cfg.Server.HTTPPort),
		Handler:      r,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 60 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	go func() {
		logger.Info("gateway HTTP server starting", zap.Int("port", cfg.Server.HTTPPort))
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatal("failed to start server", zap.Error(err))
		}
	}()

	// Graceful shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	logger.Info("gateway shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	srv.Shutdown(ctx)
}

// logSafeDSN returns a DSN with the password masked for safe logging.
// For SQLite (file paths), the value is returned as-is.
// For MySQL DSNs (user:pass@tcp(host:port)/db), the password is replaced with "***".
func logSafeDSN(dsn string) string {
	// MySQL DSN format: user:password@tcp(host:port)/db?params
	atIdx := -1
	for i := 0; i < len(dsn); i++ {
		if dsn[i] == '@' {
			atIdx = i
			break
		}
	}
	if atIdx == -1 {
		// Not a MySQL DSN (likely a SQLite file path).
		return dsn
	}
	// Find the first ':' before '@' to locate the password separator.
	colonIdx := -1
	for i := 0; i < atIdx; i++ {
		if dsn[i] == ':' {
			colonIdx = i
			break
		}
	}
	if colonIdx == -1 {
		return dsn[:atIdx] + "@***" + dsn[atIdx:]
	}
	return dsn[:colonIdx+1] + "***" + dsn[atIdx:]
}
