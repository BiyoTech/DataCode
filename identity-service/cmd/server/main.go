// Command server runs identity-service: the platform's single source of truth
// for identity, authorization and tenant placement (design 02 §2.2, ADR-007).
//
// Why this is a service and not gateway code:
//
//   - identity is GLOBAL while the gateway is per-cell, so identity tables
//     cannot travel with a cell;
//   - non-HTTP entry points (schedules, reconcilers, CLI shims) have no gateway
//     middleware and need a callable authorization API;
//   - the identity schema needs MANAGED migrations — adding a column to
//     `users` had no safe procedure while the DDL ran ad-hoc at gateway boot.
//
// Boot sequence, in order and for a reason:
//  1. open the database;
//  2. apply pending migrations (or refuse to serve — running application code
//     against an older schema is how silent corruption starts);
//  3. seed the permission catalog and built-in role templates;
//  4. serve.
package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/api"
	"github.com/datacode/identity-service/internal/auth"
	"github.com/datacode/identity-service/internal/authz"
	"github.com/datacode/identity-service/internal/config"
	"github.com/datacode/identity-service/internal/migrate"
	"github.com/datacode/identity-service/internal/placement"
	"github.com/datacode/identity-service/internal/rbac"
)

// defaultPort is identity-service's listen port. The host services occupy
// 8000-8004 contiguously: 8000 gateway, 8001 agent, 8002 workspace-runtime,
// 8003 identity-service (this one), 8004 control-service.
const defaultPort = 8003

func main() {
	logger, _ := zap.NewProduction()
	defer func() { _ = logger.Sync() }()
	// Every line carries the cell id even though identity itself is global:
	// operators correlate identity logs with the cell whose request caused
	// them, and the field must exist from day one (design 03 §7).
	logger = logger.With(zap.String("service", "identity-service"))
	zap.ReplaceGlobals(logger)

	if err := run(logger); err != nil {
		logger.Fatal("identity_service_failed", zap.Error(err))
	}
}

func run(logger *zap.Logger) error {
	cfg := config.Load()
	ctx := context.Background()

	// ── 1. Database ───────────────────────────────────────────────────
	// auth.NewStore owns the pool; every other domain (rbac, authz,
	// placement, migrations) WRAPS that same handle rather than opening a
	// second one against the same database — one pool per process, one
	// answer to "is the database reachable".
	authStore, err := auth.NewStore(auth.Dialect(cfg.Auth.AuthDialect), cfg.Auth.AuthDSN)
	if err != nil {
		return fmt.Errorf("open identity database: %w", err)
	}
	defer func() { _ = authStore.Close() }()

	db := authStore.DB()
	logger.Info("identity_database_open", zap.String("dialect", string(db.Dialect())))

	// ── 2. Migrations ─────────────────────────────────────────────────
	// AUTO_MIGRATE is opt-in: in production the deployment pipeline runs the
	// migration as a separate, observable step, and a replica that silently
	// migrated on boot would race its siblings during a rolling update.
	if autoMigrate() {
		applied, migErr := migrate.Run(ctx, db, logger)
		if migErr != nil {
			return fmt.Errorf("apply migrations: %w", migErr)
		}
		if len(applied) == 0 {
			logger.Info("migrations_already_current")
		}
	}
	pending, err := migrate.Pending(ctx, db)
	if err != nil {
		return fmt.Errorf("check pending migrations: %w", err)
	}
	if len(pending) > 0 {
		// Fail closed: serving on an older schema than the binary expects
		// produces wrong answers rather than errors.
		return fmt.Errorf(
			"database schema is behind: pending migrations %v "+
				"(run with IDENTITY_SERVICE_AUTO_MIGRATE=1 or apply them from the pipeline)",
			pending)
	}

	// ── 3. Domain services ────────────────────────────────────────────
	issuer, err := auth.NewTokenIssuer(
		os.Getenv("GW_JWT_SECRET"),
		time.Duration(cfg.Auth.AccessTokenTTLMinutes)*time.Minute,
		time.Duration(cfg.Auth.PreAuthTokenTTLMinutes)*time.Minute,
		time.Duration(cfg.Auth.RefreshTokenTTLHours)*time.Hour,
	)
	if err != nil {
		// The signing secret is env-only and mandatory: without it every token
		// this service mints would be unverifiable by the gateway.
		return fmt.Errorf("token issuer: %w", err)
	}

	limiter := auth.NewRateLimiter(
		cfg.Auth.LoginMaxAttempts,
		time.Duration(cfg.Auth.LoginLockoutMinutes)*time.Minute,
	)
	authService := auth.NewService(authStore, issuer, limiter, cfg.Auth.BcryptCost, logger)

	rbacService := rbac.NewService(rbac.NewStore(db), logger)
	if err := rbacService.Bootstrap(ctx); err != nil {
		return fmt.Errorf("bootstrap rbac: %w", err)
	}

	authzService := authz.NewService(db, rbacService)
	placementStore := placement.NewStore(db)
	// Stamp cell + perms version into every token this service issues.
	authService = authService.WithPlacement(
		placement.NewTokenResolver(placementStore, authzService))

	// ── 4. HTTP ───────────────────────────────────────────────────────
	internalToken := os.Getenv("IDENTITY_SERVICE_INTERNAL_TOKEN")
	router := api.Router(
		authService,
		api.NewAuthHandler(authService, rbacService, logger),
		api.NewRBACHandler(rbacService, logger),
		api.NewInternalHandler(
			authzService, placementStore,
			// Binding mutations: this service is the ONLY writer of
			// workspace_members, so the gateway asks it to grant/revoke
			// instead of writing its own copy of the table.
			api.NewBindingHandler(authService, rbacService, logger),
			internalToken, logger,
		),
		logger,
	)

	server := &http.Server{
		Addr:              fmt.Sprintf(":%d", port()),
		Handler:           router,
		ReadHeaderTimeout: 10 * time.Second,
	}

	shutdown := make(chan os.Signal, 1)
	signal.Notify(shutdown, os.Interrupt, syscall.SIGTERM)

	errs := make(chan error, 1)
	go func() {
		logger.Info("identity_service_started",
			zap.Int("port", port()),
			zap.Bool("internal_api", internalToken != ""))
		if serveErr := server.ListenAndServe(); serveErr != nil &&
			!errors.Is(serveErr, http.ErrServerClosed) {
			errs <- serveErr
		}
	}()

	select {
	case serveErr := <-errs:
		return serveErr
	case <-shutdown:
		logger.Info("identity_service_stopping")
		stopCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		return server.Shutdown(stopCtx)
	}
}

func port() int {
	if raw := os.Getenv("IDENTITY_SERVICE_PORT"); raw != "" {
		if parsed, err := strconv.Atoi(raw); err == nil && parsed > 0 {
			return parsed
		}
	}
	return defaultPort
}

func autoMigrate() bool {
	switch os.Getenv("IDENTITY_SERVICE_AUTO_MIGRATE") {
	case "1", "true", "TRUE", "yes":
		return true
	default:
		return false
	}
}
