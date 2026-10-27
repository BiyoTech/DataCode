package placement

import (
	"context"
	"testing"
)

// A cell registers itself at startup, so the value the global edge dials is
// written by the party that knows it. An operator-maintained table is a second
// source of truth that drifts SILENTLY: routing keeps working for tenants
// already placed and breaks only for new ones.
func TestRegisterCellCreatesAndUpdates(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	if err := store.RegisterCell(ctx, Cell{
		CellID:          "cell-1",
		Region:          "eu-west",
		Endpoint:        "https://cell-1.example",
		CapacityTenants: 200,
	}); err != nil {
		t.Fatalf("register: %v", err)
	}

	cell, found, err := store.GetCell(ctx, "cell-1")
	if err != nil || !found {
		t.Fatalf("get cell: err=%v found=%v", err, found)
	}
	if cell.Endpoint != "https://cell-1.example" {
		t.Errorf("endpoint = %q", cell.Endpoint)
	}
	if cell.Region != "eu-west" || cell.CapacityTenants != 200 {
		t.Errorf("region/capacity not stored: %+v", cell)
	}
	// Defaulted, not left empty: PickCell skips a cell whose status is not
	// "active", so an unset status would make a freshly registered cell
	// invisible to placement.
	if cell.Status != "active" {
		t.Errorf("status = %q, want active by default", cell.Status)
	}
}

// A cell re-registers on every restart, which is the common path — and a
// redeploy behind a new address must self-heal rather than add a second row.
func TestRegisterCellIsIdempotentAndUpdatesTheAddress(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		if err := store.RegisterCell(ctx, Cell{
			CellID:          "cell-1",
			Endpoint:        "https://old.example",
			CapacityTenants: 100,
		}); err != nil {
			t.Fatalf("register #%d: %v", i, err)
		}
	}
	if err := store.RegisterCell(ctx, Cell{
		CellID:          "cell-1",
		Endpoint:        "https://new.example",
		CapacityTenants: 300,
	}); err != nil {
		t.Fatalf("re-register: %v", err)
	}

	cells, err := store.ListCells(ctx)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	matching := 0
	for _, cell := range cells {
		if cell.CellID != "cell-1" {
			continue
		}
		matching++
		if cell.Endpoint != "https://new.example" {
			t.Errorf("endpoint = %q, want the re-registered address", cell.Endpoint)
		}
		if cell.CapacityTenants != 300 {
			t.Errorf("capacity = %d, want 300", cell.CapacityTenants)
		}
	}
	if matching != 1 {
		t.Fatalf("cell-1 appears %d times, want exactly one row", matching)
	}
}

// assigned_tenants is derived from the placement table, never taken from the
// cell: a cell reporting its own tenant count would let a stale or forged value
// distort placement decisions.
func TestRegisterCellDoesNotOverwriteAssignedTenants(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	if err := store.RegisterCell(ctx, Cell{
		CellID: "cell-1", Endpoint: "https://cell-1.example", CapacityTenants: 10,
	}); err != nil {
		t.Fatalf("register: %v", err)
	}
	if err := store.Assign(ctx, "t1", "cell-1"); err != nil {
		t.Fatalf("assign: %v", err)
	}

	// Re-register, as a restart would.
	if err := store.RegisterCell(ctx, Cell{
		CellID: "cell-1", Endpoint: "https://cell-1.example", CapacityTenants: 10,
	}); err != nil {
		t.Fatalf("re-register: %v", err)
	}

	cell, _, err := store.GetCell(ctx, "cell-1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if cell.AssignedTenants != 1 {
		t.Errorf("assigned_tenants = %d, want the derived 1", cell.AssignedTenants)
	}
}

func TestRegisterCellRequiresAnID(t *testing.T) {
	store := setup(t)

	if err := store.RegisterCell(context.Background(), Cell{
		Endpoint: "https://nowhere.example",
	}); err == nil {
		t.Error("a registration without a cell id must be refused")
	}
}

// An unknown cell must be reported as absent rather than as an empty cell: the
// edge needs to tell "not a cell" apart from "a cell with no address", and only
// the first is a routing error worth surfacing to the client.
func TestGetCellReportsAbsence(t *testing.T) {
	store := setup(t)

	cell, found, err := store.GetCell(context.Background(), "cell-nope")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if found {
		t.Fatalf("an unregistered cell was reported as found: %+v", cell)
	}
}

// PickCell must prefer the emptiest ACTIVE cell with room, which is what keeps
// cells balanced as they fill at different rates.
func TestPickCellPrefersTheEmptiestRegisteredCell(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	for _, cell := range []Cell{
		{CellID: "cell-0", Endpoint: "https://a", CapacityTenants: 10},
		{CellID: "cell-1", Endpoint: "https://b", CapacityTenants: 10},
	} {
		if err := store.RegisterCell(ctx, cell); err != nil {
			t.Fatalf("register %s: %v", cell.CellID, err)
		}
	}
	// Load cell-0 so cell-1 becomes the emptiest.
	for _, tenant := range []string{"t1", "t2", "t3"} {
		if err := store.Assign(ctx, tenant, "cell-0"); err != nil {
			t.Fatalf("assign %s: %v", tenant, err)
		}
	}

	picked, err := store.PickCell(ctx)
	if err != nil {
		t.Fatalf("pick: %v", err)
	}
	if picked != "cell-1" {
		t.Errorf("picked %q, want the emptier cell-1", picked)
	}
}

// A cell at capacity stops accepting NEW tenants without affecting the ones it
// already serves — which is how a cell is drained safely.
func TestPickCellSkipsFullCells(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	if err := store.RegisterCell(ctx, Cell{
		CellID: "cell-1", Endpoint: "https://b", CapacityTenants: 1,
	}); err != nil {
		t.Fatalf("register: %v", err)
	}
	if err := store.Assign(ctx, "t1", "cell-1"); err != nil {
		t.Fatalf("assign: %v", err)
	}

	picked, err := store.PickCell(ctx)
	if err != nil {
		t.Fatalf("pick: %v", err)
	}
	if picked == "cell-1" {
		t.Error("a full cell must not be picked for a new tenant")
	}
}

// capacity_tenants: 0 means "do not place new tenants here". Used to quiesce a
// cell before decommissioning it, without taking it out of service.
func TestPickCellSkipsZeroCapacityCells(t *testing.T) {
	store := setup(t)
	ctx := context.Background()

	if err := store.RegisterCell(ctx, Cell{
		CellID: "cell-quiesced", Endpoint: "https://c", CapacityTenants: 0,
	}); err != nil {
		t.Fatalf("register: %v", err)
	}

	picked, err := store.PickCell(ctx)
	if err != nil {
		t.Fatalf("pick: %v", err)
	}
	if picked == "cell-quiesced" {
		t.Error("a zero-capacity cell must not receive new tenants")
	}
}
