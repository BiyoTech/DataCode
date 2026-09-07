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
	"github.com/datacode/gateway/internal/authz"
	"github.com/datacode/gateway/internal/cellrouter"
	"github.com/datacode/gateway/internal/cellx"
	"github.com/datacode/gateway/internal/config"
	"github.com/datacode/gateway/internal/leaserouter"
	"github.com/datacode/gateway/internal/redisx"
	"github.com/datacode/gateway/internal/router"
)

// redisPingTimeout bounds the startup reachability probe. Redis is a
// degradable dependency, so waiting on it must never delay the listener.
const redisPingTimeout = 3 * time.Second

func main() {
	logger, _ := zap.NewProduction()
	defer logger.Sync()
	// Expose the logger globally so infrastructure code that runs before any
	// service is wired (e.g. schema migration inside the store constructor)
	// can still report what it does.
	//
	// Every log line this replica emits carries its cell id (design 03, P1):
	// multi-cell operations must be able to filter by cell from day one,
	// even while only cell-0 exists.
	logger = logger.With(zap.String("cell_id", cellx.CellID()))
	zap.ReplaceGlobals(logger)

	cfg := config.Load()

	// Token verification is mandatory: without the signing secret the gateway
	// cannot verify the identities identity-service minted, so fail fast
	// instead of starting in an insecure state.
	//
	// The gateway only VERIFIES. Issuing (login, refresh, tenant selection)
	// belongs to identity-service, which signs with the same secret (P5.5).
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

	// Coordination backend. It is optional by design: the gateway degrades to
	// per-replica state rather than refusing to serve, so an unreachable Redis
	// is reported and startup continues.
	coordinator := redisx.New(cfg.Platform, logger)
	defer coordinator.Close()
	if coordinator.Enabled() {
		pingCtx, cancel := context.WithTimeout(context.Background(), redisPingTimeout)
		if err := coordinator.Ping(pingCtx); err != nil {
			logger.Warn("redis unreachable at startup",
				zap.Error(err),
				zap.String("impact", "cross-replica coordination degraded to per-replica state"))
		} else {
			logger.Info("redis ready")
		}
		cancel()
	}

	// Identity is a hard dependency since P5.5: the gateway owns no identity
	// table any more, so a missing endpoint means it can neither authorize nor
	// serve the proxied /auth surface. Reported loudly rather than fatally so
	// /health stays reachable for an operator diagnosing the misconfiguration.
	identityToken := os.Getenv("IDENTITY_SERVICE_INTERNAL_TOKEN")

	// Authorization snapshots come from identity-service (P2, ADR-007): the
	// gateway keeps the decision logic and gives up ownership of the data, so
	// identity can be global while gateways are per-cell.
	//
	// Not configured => the resolver reports every lookup as unavailable and
	// Authorize denies. That is intentional: a gateway that cannot establish
	// permissions must not serve.
	authzResolver := authz.NewResolver(
		cfg.Services.IdentityServiceURL,
		identityToken,
		time.Duration(cfg.Auth.AuthzCacheTTLSeconds)*time.Second,
		logger,
	)

	// Binding mutations (workspace member grant/revoke/purge) are performed BY
	// identity-service. The gateway decides whether they are allowed and then
	// delegates the write, so the identity dataset keeps exactly one writer —
	// the precondition for the P7 Global/Cell database split.
	bindings := authz.NewBindings(
		cfg.Services.IdentityServiceURL, identityToken, logger)

	if authzResolver.Enabled() {
		logger.Info("identity_client_configured",
			zap.String("identity_url", cfg.Services.IdentityServiceURL),
			zap.Int("authz_cache_ttl_seconds", cfg.Auth.AuthzCacheTTLSeconds))
	} else {
		logger.Error("identity_client_disabled",
			zap.String("reason", "identity service url or internal token is unset"),
			zap.String("effect", "every authorization-requiring route is denied and "+
				"the proxied /auth surface is unavailable"),
			zap.String("hint", "set services.identity_service_url and "+
				"IDENTITY_SERVICE_INTERNAL_TOKEN"))
	}

	// Lease-aware routing (design 06 P4a). A working copy is owned by exactly
	// one workspace-runtime replica; without this the gateway would load
	// balance requests for it and put a second writer on one Git worktree.
	//
	// On a lease miss the gateway asks the runtime fleet to PLACE the lease
	// (any replica receives it; the runtime's placement service picks the
	// winning instance and has it commit the lease). Without a configured
	// runtime URL, misses degrade to the stable-hash path (design 05 §5.2).
	var acquirer *leaserouter.HTTPAcquirer
	if cfg.Services.RuntimeServiceURL != "" {
		acquirer = leaserouter.NewHTTPAcquirer(
			cfg.Services.RuntimeServiceURL,
			os.Getenv("WORKSPACE_RUNTIME_INTERNAL_TOKEN"),
			logger,
		)
		if !acquirer.Enabled() {
			logger.Warn("lease_acquirer_disabled",
				zap.String("reason", "no workspace-runtime URL configured"))
		}
	}
	leaseRouter := leaserouter.New(
		coordinator, acquirer, cfg.Services.RuntimeServiceURL, logger)
	if leaseRouter.Enabled() {
		logger.Info("lease_routing_enabled",
			zap.Duration("cache_ttl", leaserouter.CacheTTL),
			zap.Bool("placement", acquirer.Enabled()))
	} else {
		logger.Warn("lease_routing_disabled",
			zap.String("reason", "redis is not configured"),
			zap.String("effect", "working-copy requests are load balanced; "+
				"safe only while runtime runs a single replica"))
	}

	// Cell routing (design 06 P7). This gateway serves ONE cell; a request whose
	// tenant lives elsewhere is answered 421 with that cell's address rather
	// than proxied — forwarding would make this cell's availability depend on
	// the other one's, which is the coupling cells exist to remove.
	//
	// The directory is read from the GLOBAL registry (identity-service) and is
	// used only to name the correct address for a rejected request; local
	// traffic never consults it, so a global outage does not affect this cell's
	// own tenants.
	cellDirectory := cellrouter.NewRegistryDirectory(
		cfg.Services.IdentityServiceURL,
		identityToken,
		cellrouter.DefaultDirectoryTTL,
		cellrouter.DefaultDirectoryTimeout,
		logger,
	)
	cellRouter := cellrouter.New(cellx.CellID(), cellDirectory, logger)
	logger.Info("cell_routing_ready",
		zap.String("local_cell", cellRouter.LocalCell()),
		zap.Bool("directory_enabled", cellDirectory.Enabled()))

	r := router.Setup(
		cfg, logger, issuer, authzResolver, bindings, leaseRouter, cellRouter)

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
