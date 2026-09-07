package placement

import (
	"context"
	"path/filepath"
	"testing"

	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/dbx"
	"github.com/datacode/identity-service/internal/migrate"
)

func setup(t *testing.T) *Store {
	t.Helper()
	db, err := dbx.Open(dbx.SQLite, filepath.Join(t.TempDir(), "identity.db"))
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if _, err := migrate.Run(context.Background(), db, zap.NewNop()); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return NewStore(db)
}

// A tenant with no row must resolve to the seeded default rather than error:
// that is what lets a single-cell deployment run with an untouched table, and
// what keeps login working for tenants created before cells existed.
func TestGetDefaultsForUnplacedTenant(t *testing.T) {
	store := setup(t)

	p, err := store.Get(context.Background(), "never-placed")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if p.CellID != DefaultCellID {
		t.Errorf("cell = %q, want %q", p.CellID, DefaultCellID)
	}
	if !p.Routable() {
		t.Error("an unplaced tenant must be routable")
	}
}

func TestAssignIsIdempotentAndRecounts(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		if err := store.Assign(ctx, "t1", DefaultCellID); err != nil {
			t.Fatalf("assign #%d: %v", i, err)
		}
	}

	cells, err := store.ListCells(ctx)
	if err != nil {
		t.Fatalf("list cells: %v", err)
	}
	for _, c := range cells {
		if c.CellID != DefaultCellID {
			continue
		}
		// Counters are recomputed, so repeated assignment cannot inflate them.
		if c.AssignedTenants != 1 {
			t.Errorf("assigned_tenants = %d after 3 identical assignments, want 1",
				c.AssignedTenants)
		}
	}
}

// A migrating tenant must stop being routable: a token minted mid-migration
// would carry a cell that stops serving it during the session.
func TestMigrationStateControlsRoutability(t *testing.T) {
	store := setup(t)
	ctx := context.Background()
	if err := store.Assign(ctx, "t1", DefaultCellID); err != nil {
		t.Fatalf("assign: %v", err)
	}

	if err := store.SetMigrationState(ctx, "t1", StateDraining, "cell-1"); err != nil {
		t.Fatalf("set draining: %v", err)
	}
	p, err := store.Get(ctx, "t1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if p.Routable() {
		t.Error("a draining tenant must not be routable")
	}
	if p.TargetCellID != "cell-1" {
		t.Errorf("target cell = %q, want cell-1", p.TargetCellID)
	}
	// The CURRENT cell must be unchanged: the data has not moved yet.
	if p.CellID != DefaultCellID {
		t.Errorf("cell = %q during migration, want the source %q", p.CellID, DefaultCellID)
	}

	// Returning to stable clears the target, otherwise the next migration
	// would be ambiguous.
	if err := store.SetMigrationState(ctx, "t1", StateStable, ""); err != nil {
		t.Fatalf("set stable: %v", err)
	}
	p, _ = store.Get(ctx, "t1")
	if p.TargetCellID != "" {
		t.Errorf("target cell = %q after returning to stable, want empty", p.TargetCellID)
	}
	if !p.Routable() {
		t.Error("a stable tenant must be routable")
	}
}

func TestSetMigrationStateValidates(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	if err := store.SetMigrationState(ctx, "t1", "teleporting", "cell-1"); err == nil {
		t.Error("an unknown state must be rejected")
	}
	if err := store.SetMigrationState(ctx, "t1", StateMigrating, ""); err == nil {
		t.Error("a staged state without a target must be rejected")
	}
}

func TestPickCellChoosesLeastUtilized(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	// Only cell-0 is seeded and it is empty, so it must be picked.
	cell, err := store.PickCell(ctx)
	if err != nil {
		t.Fatalf("pick: %v", err)
	}
	if cell != DefaultCellID {
		t.Errorf("picked %q, want %q", cell, DefaultCellID)
	}
}

// EnsurePlaced must not move a tenant that already has a placement, so a
// retried registration cannot relocate live data.
func TestEnsurePlacedDoesNotMoveExistingTenant(t *testing.T) {
	store := setup(t)
	ctx := context.Background()
	resolver := NewTokenResolver(store, nil)

	if err := store.Assign(ctx, "t1", "cell-9"); err != nil {
		t.Fatalf("assign: %v", err)
	}
	if err := resolver.EnsurePlaced(ctx, "t1"); err != nil {
		t.Fatalf("ensure placed: %v", err)
	}
	p, _ := store.Get(ctx, "t1")
	if p.CellID != "cell-9" {
		t.Errorf("cell = %q, want the pre-existing cell-9", p.CellID)
	}
}

// A nil version reader must not break token stamping: the cell is the
// load-bearing claim, the version only costs a cache generation.
func TestPlacementForTokenToleratesMissingVersionReader(t *testing.T) {
	store := setup(t)
	resolver := NewTokenResolver(store, nil)

	cell, version, err := resolver.PlacementForToken(context.Background(), "t1")
	if err != nil {
		t.Fatalf("placement for token: %v", err)
	}
	if cell != DefaultCellID {
		t.Errorf("cell = %q, want %q", cell, DefaultCellID)
	}
	if version != 0 {
		t.Errorf("version = %d, want 0 when no reader is wired", version)
	}
}
