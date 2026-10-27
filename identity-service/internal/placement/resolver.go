// Package placement — token stamping adapter.
//
// Bridges the auth service's narrow PlacementResolver interface to the two
// tables that actually answer it: `tenant_placement` (which cell) and
// `tenant_perms_version` (authorization generation). Keeping the adapter here
// rather than in auth is what lets authentication compile and run without any
// notion of cells.
package placement

import (
	"context"
	"fmt"
)

// VersionReader supplies a tenant's authorization generation.
//
// Implemented by authz.Service; declared here to avoid an import cycle
// (authz depends on rbac, which has no reason to know about placement).
type VersionReader interface {
	PermsVersion(ctx context.Context, tenantID string) (int64, error)
}

// TokenResolver answers "what should this tenant's token carry".
type TokenResolver struct {
	store    *Store
	versions VersionReader
}

func NewTokenResolver(store *Store, versions VersionReader) *TokenResolver {
	return &TokenResolver{store: store, versions: versions}
}

// PlacementForToken returns the cell id and perms version for a tenant.
//
// A tenant mid-migration is reported with its CURRENT cell, never the target:
// the token must route to the cell that still holds the data. Refusing to
// issue during a migration window is the caller's decision (identity answers
// 503 on login), not this resolver's.
func (r *TokenResolver) PlacementForToken(
	ctx context.Context, tenantID string,
) (string, int64, error) {
	p, err := r.store.Get(ctx, tenantID)
	if err != nil {
		return "", 0, fmt.Errorf("resolve placement: %w", err)
	}

	var version int64
	if r.versions != nil {
		version, err = r.versions.PermsVersion(ctx, tenantID)
		if err != nil {
			// The cell is the load-bearing claim; a missing version only
			// costs a cache generation, so it must not fail the login.
			return p.CellID, 0, nil
		}
	}
	return p.CellID, version, nil
}

// EnsurePlaced assigns a cell to a tenant that has none yet.
//
// Called after tenant creation so a new tenant lands on the emptiest active
// cell instead of implicitly defaulting; idempotent, so a retried
// registration cannot move an existing tenant.
func (r *TokenResolver) EnsurePlaced(ctx context.Context, tenantID string) error {
	p, err := r.store.Get(ctx, tenantID)
	if err != nil {
		return err
	}
	// Get() synthesizes the default for a missing row; distinguish a real row
	// by re-reading with an explicit existence check.
	exists, err := r.store.exists(ctx, tenantID)
	if err != nil {
		return err
	}
	if exists {
		return nil
	}
	cellID, err := r.store.PickCell(ctx)
	if err != nil {
		return err
	}
	if cellID == "" {
		cellID = p.CellID
	}
	return r.store.Assign(ctx, tenantID, cellID)
}
