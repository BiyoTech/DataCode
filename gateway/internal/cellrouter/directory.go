// Package cellrouter — directory backed by the global cell registry.
//
// The registry lives in the Global database and is served by identity-service
// (`GET /internal/cells`). This gateway reads it, caches it, and uses it only to
// tell a misdirected client where to go — never on the path of a request it
// serves itself. That is why a stale or unavailable directory degrades to "I
// cannot name the right cell" rather than to a failure: refusing a LOCAL request
// because the registry is down would make every cell depend on the global tier
// for its own traffic, which is the coupling cells exist to remove.
package cellrouter

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.uber.org/zap"
)

// Cell is one entry of the global registry, reduced to what routing needs.
type Cell struct {
	CellID   string `json:"cell_id"`
	Endpoint string `json:"endpoint"`
	Status   string `json:"status"`
}

// Directory defaults. A cell's address changes on the order of deployments, not
// requests, so a minute of staleness costs nothing — and this value is only ever
// used to redirect a client that has already been told it is in the wrong place.
const (
	DefaultDirectoryTTL     = time.Minute
	DefaultDirectoryTimeout = 5 * time.Second
)

// RegistryDirectory caches the cell inventory from identity-service.
type RegistryDirectory struct {
	identityURL string
	token       string
	ttl         time.Duration
	timeout     time.Duration
	client      *http.Client
	logger      *zap.Logger

	mu        sync.RWMutex
	cells     map[string]Cell
	refreshed time.Time
}

// NewRegistryDirectory builds a directory. An empty URL or token yields a
// directory that never resolves anything, which the router handles: it then
// refuses foreign requests without naming an address.
func NewRegistryDirectory(
	identityURL, token string, ttl, timeout time.Duration, logger *zap.Logger,
) *RegistryDirectory {
	return &RegistryDirectory{
		identityURL: strings.TrimSuffix(identityURL, "/"),
		token:       token,
		ttl:         ttl,
		timeout:     timeout,
		client:      &http.Client{Timeout: timeout},
		logger:      logger,
		cells:       make(map[string]Cell),
	}
}

// Enabled reports whether the directory can reach the registry at all.
func (d *RegistryDirectory) Enabled() bool {
	return d.identityURL != "" && d.token != ""
}

// Endpoint returns a cell's client-facing address.
//
// Serves from cache, refreshing when stale. A refresh failure keeps the previous
// contents: a cell's address changes rarely, so yesterday's answer is far more
// useful than none — and this value is only used to redirect a client that has
// already been told it is in the wrong place.
func (d *RegistryDirectory) Endpoint(cellID string) (string, bool) {
	if cellID == "" {
		return "", false
	}
	if d.stale() {
		d.refresh()
	}

	d.mu.RLock()
	defer d.mu.RUnlock()
	cell, ok := d.cells[cellID]
	if !ok || cell.Endpoint == "" {
		return "", ok
	}
	return cell.Endpoint, true
}

// Status returns a cell's registry status, and whether the cell is known.
//
// Satisfies MaintenanceProbe (consumed by the Router through that narrow
// interface, so the CellDirectory contract stays at one method). Unknown means
// either "not registered" or "registry unreachable": the router treats both as
// "not in maintenance", keeping local availability decoupled from the global
// tier — the same degradation rule Endpoint documents.
func (d *RegistryDirectory) Status(cellID string) (string, bool) {
	if cellID == "" {
		return "", false
	}
	if d.stale() {
		d.refresh()
	}

	d.mu.RLock()
	defer d.mu.RUnlock()
	cell, ok := d.cells[cellID]
	if !ok {
		return "", false
	}
	return cell.Status, true
}

func (d *RegistryDirectory) stale() bool {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return time.Since(d.refreshed) > d.ttl
}

// refresh reloads the inventory. Best-effort by contract (see Endpoint).
func (d *RegistryDirectory) refresh() {
	if !d.Enabled() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), d.timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(
		ctx, http.MethodGet, d.identityURL+"/internal/cells", nil)
	if err != nil {
		return
	}
	req.Header.Set("X-Internal-Token", d.token)

	response, err := d.client.Do(req)
	if err != nil {
		d.logger.Warn("cell_directory_refresh_failed",
			zap.String("identity_url", d.identityURL), zap.Error(err),
			zap.String("effect", "misdirected clients cannot be told where to go; "+
				"local traffic is unaffected"))
		// Mark the attempt so a hard-down registry is not retried on every
		// request; the previous contents keep serving.
		d.mu.Lock()
		d.refreshed = time.Now()
		d.mu.Unlock()
		return
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		d.logger.Warn("cell_directory_refresh_rejected",
			zap.Int("status", response.StatusCode))
		d.mu.Lock()
		d.refreshed = time.Now()
		d.mu.Unlock()
		return
	}

	var body struct {
		Cells []Cell `json:"cells"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		d.logger.Warn("cell_directory_decode_failed", zap.Error(err))
		return
	}

	cells := make(map[string]Cell, len(body.Cells))
	for _, cell := range body.Cells {
		if cell.CellID == "" {
			continue
		}
		cells[cell.CellID] = cell
	}

	d.mu.Lock()
	d.cells = cells
	d.refreshed = time.Now()
	d.mu.Unlock()
}

// Cells returns the cached inventory, for /health.
func (d *RegistryDirectory) Cells() []Cell {
	d.mu.RLock()
	defer d.mu.RUnlock()
	out := make([]Cell, 0, len(d.cells))
	for _, cell := range d.cells {
		out = append(out, cell)
	}
	return out
}
