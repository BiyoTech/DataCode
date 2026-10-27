// Package redisx wraps the shared coordination backend for the gateway.
//
// Redis holds the state that must be identical across gateway replicas — today
// login rate limiting, later general API quotas. It is deliberately NOT a
// datastore for the gateway: nothing here is a source of truth, so an
// unreachable Redis must degrade to the in-process implementation rather than
// fail requests.
//
// Two invariants this package exists to enforce:
//
//  1. One pool per process. A pool per domain would multiply sockets against
//     the same instance and make "is Redis up" have several answers at once.
//  2. One namespace. Every key is prefixed from platform.yaml so a single
//     instance can host several environments without collisions.
package redisx

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"

	"github.com/datacode/gateway/internal/cellx"
	"github.com/datacode/gateway/internal/config"
)

// Client is the gateway's handle on the coordination backend.
//
// A nil *Client is a valid, fully usable value meaning "Redis is disabled";
// callers check Enabled() and fall back. This keeps the disabled path free of
// nil-pointer special cases at every call site.
type Client struct {
	rdb       *redis.Client
	keyPrefix string
	logger    *zap.Logger
}

// New builds the client from platform configuration.
//
// It does NOT dial: go-redis connects lazily, and blocking gateway startup on
// an optional backend would turn a degradable dependency into a hard one. Use
// Ping to check reachability once the server is up.
//
// Returns nil when Redis is disabled, in which case every method is a no-op
// and Enabled() reports false.
func New(platform *config.PlatformConfig, logger *zap.Logger) *Client {
	if !platform.RedisEnabled() {
		logger.Warn("redis_disabled",
			zap.String("impact", "cross-replica coordination is off; "+
				"per-replica fallbacks are only correct for a single replica"))
		return nil
	}

	cfg := platform.Redis
	rdb := redis.NewClient(&redis.Options{
		Addr: fmt.Sprintf("%s:%d", cfg.Host, cfg.Port),
		// Password is env-only; it is never present in platform.yaml.
		Password:     os.Getenv("REDIS_PASSWORD"),
		DB:           cfg.DB,
		DialTimeout:  seconds(cfg.ConnectTimeoutSeconds, 3),
		ReadTimeout:  seconds(cfg.OperationTimeoutSeconds, 3),
		WriteTimeout: seconds(cfg.OperationTimeoutSeconds, 3),
		PoolSize:     poolSize(cfg.MaxConnections, 32),
	})

	// The cell segment is part of the prefix, not a Key() argument, so no
	// domain can accidentally build an un-cell-scoped key (design 03, P1).
	effectivePrefix := cfg.KeyPrefix + ":" + cellx.CellID()

	logger.Info("redis_client_created",
		zap.String("addr", fmt.Sprintf("%s:%d", cfg.Host, cfg.Port)),
		zap.Int("db", cfg.DB),
		zap.String("key_prefix", effectivePrefix))

	return &Client{rdb: rdb, keyPrefix: effectivePrefix, logger: logger}
}

// Enabled reports whether the coordination backend is configured.
func (c *Client) Enabled() bool { return c != nil && c.rdb != nil }

// Redis exposes the underlying handle for domains that need commands or
// scripts this wrapper does not model. Returns nil when disabled.
func (c *Client) Redis() *redis.Client {
	if !c.Enabled() {
		return nil
	}
	return c.rdb
}

// Key joins segments into a fully namespaced key, e.g.
// Key("auth", "login", user) -> "datacode:auth:login:<user>".
func (c *Client) Key(segments ...string) string {
	if !c.Enabled() {
		return strings.Join(segments, ":")
	}
	return strings.Join(append([]string{c.keyPrefix}, segments...), ":")
}

// Get reads a namespaced key.
//
// A missing key returns ("", nil), not an error: absence is a normal, expected
// answer for coordination state (no lease held yet, no heartbeat published), and
// making callers distinguish redis.Nil from a real failure at every site is how
// an outage ends up being mistaken for a miss.
func (c *Client) Get(ctx context.Context, segments ...string) (string, error) {
	if !c.Enabled() {
		return "", fmt.Errorf("redisx: disabled")
	}
	value, err := c.rdb.Get(ctx, c.Key(segments...)).Result()
	if err == redis.Nil {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return value, nil
}

// SMembers reads a namespaced set. An absent set is an empty slice, for the
// same reason Get treats a missing key as empty.
func (c *Client) SMembers(ctx context.Context, segments ...string) ([]string, error) {
	if !c.Enabled() {
		return nil, fmt.Errorf("redisx: disabled")
	}
	members, err := c.rdb.SMembers(ctx, c.Key(segments...)).Result()
	if err == redis.Nil {
		return nil, nil
	}
	return members, err
}

// Ping reports whether Redis answers right now.
//
// Deliberately uncached: a cached failure would keep a recovered instance out
// of service, and a cached success would hide an outage until the next
// command. Callers use it at startup for an explicit health signal.
func (c *Client) Ping(ctx context.Context) error {
	if !c.Enabled() {
		return nil
	}
	return c.rdb.Ping(ctx).Err()
}

// Close releases the pool. Safe on a disabled client.
func (c *Client) Close() error {
	if !c.Enabled() {
		return nil
	}
	return c.rdb.Close()
}

// seconds converts a fractional-seconds config value to a duration, applying
// fallback when the value is unset or nonsensical. A zero timeout in go-redis
// means "no timeout", which is never what an operator intends here.
func seconds(value float64, fallback float64) time.Duration {
	if value <= 0 {
		value = fallback
	}
	return time.Duration(value * float64(time.Second))
}

// poolSize guards against a zero/negative configured pool size.
func poolSize(value, fallback int) int {
	if value <= 0 {
		return fallback
	}
	return value
}
