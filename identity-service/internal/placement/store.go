// Package placement owns the GLOBAL tenant -> cell routing table
// (design 03 §2.4, ADR-006).
//
// Explicit mapping, not consistent hashing: tenant sizes differ by orders of
// magnitude so a hash cannot balance them, a dedicated cell must be
// expressible for compliance/performance isolation, and a migration has to be
// controllable one tenant at a time.
//
// The global edge routes on the `cell_id` claim inside the JWT, so this table
// is read on the LOGIN path only — never on the hot request path.
package placement

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/datacode/identity-service/internal/dbx"
)

// DefaultCellID is the cell a tenant resolves to when it has no explicit
// placement row. A single-cell deployment therefore needs no setup at all,
// which is what makes the M0 -> M2 progression a pure operations change
// (design 03 §7).
const DefaultCellID = "cell-0"

// Migration states of a placement. A tenant is only routable while `stable`;
// the others describe the staged hand-over to `target_cell_id`.
const (
	StateStable    = "stable"
	StateDraining  = "draining"
	StateMigrating = "migrating"
	StateVerifying = "verifying"
)

// Placement is one tenant's cell assignment.
type Placement struct {
	TenantID       string `json:"tenant_id"`
	CellID         string `json:"cell_id"`
	TargetCellID   string `json:"target_cell_id,omitempty"`
	MigrationState string `json:"migration_state"`
}

// Routable reports whether new tokens may be issued for this tenant.
//
// False during a migration window: a token minted then would carry a cell id
// that stops serving the tenant mid-session, which is worse than making the
// client retry (identity answers 503 + Retry-After instead).
func (p Placement) Routable() bool {
	return p.MigrationState == StateStable || p.MigrationState == ""
}

// Cell is one deployment unit serving a set of tenants.
type Cell struct {
	CellID          string `json:"cell_id"`
	Region          string `json:"region"`
	Endpoint        string `json:"endpoint"`
	Status          string `json:"status"`
	CapacityTenants int    `json:"capacity_tenants"`
	AssignedTenants int    `json:"assigned_tenants"`
}

// Store reads and writes placements.
type Store struct {
	db *dbx.DB
}

func NewStore(db *dbx.DB) *Store { return &Store{db: db} }

// Get returns a tenant's placement, defaulting to the seeded cell.
//
// A missing row is NOT an error: it is how a tenant created before cells
// existed (or in a single-cell deployment) is interpreted. Returning the
// default keeps login working without a backfill.
func (s *Store) Get(ctx context.Context, tenantID string) (Placement, error) {
	row := s.db.QueryRowContext(ctx, 
		`SELECT cell_id, target_cell_id, migration_state
		   FROM tenant_placement WHERE tenant_id = ?`, tenantID)

	var p Placement
	p.TenantID = tenantID
	err := row.Scan(&p.CellID, &p.TargetCellID, &p.MigrationState)
	if errors.Is(err, sql.ErrNoRows) {
		return Placement{
			TenantID:       tenantID,
			CellID:         DefaultCellID,
			MigrationState: StateStable,
		}, nil
	}
	if err != nil {
		return Placement{}, fmt.Errorf("read placement: %w", err)
	}
	return p, nil
}

// Assign places a tenant on a cell, creating or replacing its row, and keeps
// the per-cell counters in step.
//
// Counters are derived data recomputed from the placement table rather than
// incremented blindly, so a retried or duplicated assignment cannot inflate
// them.
func (s *Store) Assign(ctx context.Context, tenantID, cellID string) error {
	now := time.Now().UTC().Format(time.RFC3339)

	res, err := s.db.ExecContext(ctx, 
		`UPDATE tenant_placement
		    SET cell_id = ?, target_cell_id = '', migration_state = ?, updated_at = ?
		  WHERE tenant_id = ?`, cellID, StateStable, now, tenantID)
	if err != nil {
		return fmt.Errorf("update placement: %w", err)
	}
	if affected, _ := res.RowsAffected(); affected == 0 {
		if _, err := s.db.ExecContext(ctx, 
			`INSERT INTO tenant_placement
			   (tenant_id, cell_id, target_cell_id, migration_state, created_at, updated_at)
			 VALUES (?, ?, '', ?, ?, ?)`,
			tenantID, cellID, StateStable, now, now); err != nil {
			return fmt.Errorf("insert placement: %w", err)
		}
	}
	return s.recountCells(ctx)
}

// SetMigrationState advances a placement through the migration state machine.
//
// `target` is required for the staged states and cleared when returning to
// stable — a stable placement with a lingering target would make the next
// migration ambiguous.
func (s *Store) SetMigrationState(ctx context.Context, tenantID, state, target string) error {
	switch state {
	case StateStable, StateDraining, StateMigrating, StateVerifying:
	default:
		return fmt.Errorf("unknown migration state %q", state)
	}
	if state != StateStable && target == "" {
		return fmt.Errorf("state %q requires a target cell", state)
	}
	if state == StateStable {
		target = ""
	}

	now := time.Now().UTC().Format(time.RFC3339)
	_, err := s.db.ExecContext(ctx, 
		`UPDATE tenant_placement
		    SET migration_state = ?, target_cell_id = ?, updated_at = ?
		  WHERE tenant_id = ?`, state, target, now, tenantID)
	return err
}

// ListCells returns every cell with its current load.
func (s *Store) ListCells(ctx context.Context) ([]Cell, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT cell_id, region, endpoint, status, capacity_tenants, assigned_tenants
		   FROM cells ORDER BY cell_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Cell
	for rows.Next() {
		var c Cell
		if err := rows.Scan(&c.CellID, &c.Region, &c.Endpoint, &c.Status,
			&c.CapacityTenants, &c.AssignedTenants); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// PickCell chooses the emptiest active cell with room left.
//
// Least-utilized rather than round-robin: cells are long-lived and tenants
// are never removed in normal operation, so utilization is the only measure
// that keeps them balanced as they fill at different rates.
func (s *Store) PickCell(ctx context.Context) (string, error) {
	cells, err := s.ListCells(ctx)
	if err != nil {
		return "", err
	}
	best := ""
	bestUtil := 2.0
	for _, c := range cells {
		if c.Status != "active" || c.CapacityTenants <= 0 {
			continue
		}
		util := float64(c.AssignedTenants) / float64(c.CapacityTenants)
		if util >= 1.0 {
			continue // full: stop accepting new tenants, alert on capacity
		}
		if util < bestUtil {
			best, bestUtil = c.CellID, util
		}
	}
	if best == "" {
		// No capacity anywhere. Returning the default keeps a single-cell
		// deployment (where `cells` may be untouched) working; a real
		// multi-cell deployment surfaces this through the capacity alert.
		return DefaultCellID, nil
	}
	return best, nil
}

// RegisterCell records or updates one cell's routable endpoint and capacity.
//
// Called by each cell's own control plane at startup, so `cells.endpoint` holds
// a value the global edge can actually dial. The alternative — an operator
// filling the table by hand — is a second source of truth that drifts the first
// time a cell's address changes, and it drifts SILENTLY: routing keeps working
// for the tenants already placed and breaks only for new ones.
//
// `assigned_tenants` is deliberately NOT taken from the caller: it is derived
// from the placement table (see recountCells), and a cell reporting its own
// tenant count would let a stale or malicious value distort placement.
func (s *Store) RegisterCell(ctx context.Context, c Cell) error {
	if c.CellID == "" {
		return errors.New("cell_id is required to register a cell")
	}
	if c.Status == "" {
		c.Status = "active"
	}
	now := time.Now().UTC().Format(time.RFC3339)

	// Upsert without relying on dialect-specific ON CONFLICT syntax: this store
	// serves sqlite, mysql and postgres, and the update path is the common one
	// (a cell re-registers on every restart).
	result, err := s.db.ExecContext(ctx,
		`UPDATE cells
		    SET region = ?, endpoint = ?, status = ?, capacity_tenants = ?, updated_at = ?
		  WHERE cell_id = ?`,
		c.Region, c.Endpoint, c.Status, c.CapacityTenants, now, c.CellID)
	if err != nil {
		return err
	}
	if affected, err := result.RowsAffected(); err == nil && affected > 0 {
		return nil
	}

	_, err = s.db.ExecContext(ctx,
		`INSERT INTO cells
		     (cell_id, region, endpoint, status, capacity_tenants, assigned_tenants,
		      created_at, updated_at)
		 VALUES (?, ?, ?, ?, ?, 0, ?, ?)`,
		c.CellID, c.Region, c.Endpoint, c.Status, c.CapacityTenants, now, now)
	if err != nil {
		// A concurrent registration by a second replica of the same cell won.
		// That is the intended outcome, not an error: both wrote the same
		// endpoint, and the row now exists.
		var exists int
		if probe := s.db.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM cells WHERE cell_id = ?`, c.CellID,
		).Scan(&exists); probe == nil && exists > 0 {
			return nil
		}
		return err
	}
	return nil
}

// GetCell returns one cell, or false when it is not registered.
//
// The global edge needs this to answer "where do I send this tenant": a cell id
// in a token that no longer has a row must produce an explicit failure rather
// than a guess.
func (s *Store) GetCell(ctx context.Context, cellID string) (Cell, bool, error) {
	var c Cell
	err := s.db.QueryRowContext(ctx,
		`SELECT cell_id, region, endpoint, status, capacity_tenants, assigned_tenants
		   FROM cells WHERE cell_id = ?`, cellID,
	).Scan(&c.CellID, &c.Region, &c.Endpoint, &c.Status,
		&c.CapacityTenants, &c.AssignedTenants)
	if errors.Is(err, sql.ErrNoRows) {
		return Cell{}, false, nil
	}
	if err != nil {
		return Cell{}, false, err
	}
	return c, true, nil
}

// exists reports whether a tenant has an explicit placement row.
//
// Distinct from Get(), which synthesizes the default cell for a missing row:
// callers that must not overwrite an existing assignment need to tell the two
// cases apart.
func (s *Store) exists(ctx context.Context, tenantID string) (bool, error) {
	var count int
	if err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM tenant_placement WHERE tenant_id = ?`, tenantID,
	).Scan(&count); err != nil {
		return false, err
	}
	return count > 0, nil
}

// recountCells recomputes assigned_tenants from the placement table.
func (s *Store) recountCells(ctx context.Context) error {
	now := time.Now().UTC().Format(time.RFC3339)
	_, err := s.db.ExecContext(ctx,
		`UPDATE cells SET assigned_tenants = (
		     SELECT COUNT(*) FROM tenant_placement WHERE tenant_placement.cell_id = cells.cell_id
		 ), updated_at = ?`, now)
	return err
}
