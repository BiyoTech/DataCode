// Package cellrouter decides whether THIS cell may serve a request, and where
// to send it when it may not (design 03 §2.4, design 06 P7).
//
// # The routing rule
//
// A tenant lives in exactly one cell. The access token carries that cell's id
// (issued by identity-service from the placement table), so the decision needs
// no lookup on the hot path:
//
//	token.cell_id == this cell   -> serve locally
//	token.cell_id != this cell   -> 421 Misdirected Request + the right address
//	token has no cell_id         -> serve locally (see below)
//
// # Why 421 and not a reverse proxy
//
// Forwarding a foreign cell's request would work, and it is the wrong thing:
//
//   - the whole point of cells is that one cell's failure cannot affect
//     another's tenants. A gateway that proxies into cell-1 makes cell-0's
//     availability depend on cell-1's — the blast radius is back;
//   - the request would traverse two edges, doubling the latency of every call
//     for a tenant that reached the wrong address;
//   - it hides a misrouting bug. With 421 the client is TOLD the correct
//     address, follows it, and the global edge's routing table is visibly wrong
//     until fixed, instead of being permanently papered over.
//
// 421 is the status HTTP defines for exactly this: "this server is not
// authoritative for the requested resource". The response carries the cell's
// endpoint so a client can retry without a discovery round trip.
//
// # Tokens with no cell claim
//
// Served locally. They are tokens minted before placement existed, or for a
// tenant that has none. Rejecting them would lock out every existing session on
// upgrade; serving them is correct because a single-cell install is exactly the
// case where every tenant is here.
package cellrouter

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync"

	"go.uber.org/zap"
)

// CellDirectory resolves a cell id to the address a client should use.
//
// Narrow on purpose: the router needs one lookup, and keeping the interface to
// that shape lets it be satisfied by the identity client, a static map from
// configuration, or a test double.
type CellDirectory interface {
	// Endpoint returns the client-facing base URL of a cell. The boolean
	// reports whether the cell is known at all — an unknown cell id must NOT
	// be answered with an empty string, because "no address" and "not a cell"
	// need different responses.
	Endpoint(cellID string) (string, bool)
}

// Router answers "may this cell serve this request".
type Router struct {
	localCell string
	directory CellDirectory
	logger    *zap.Logger

	mu       sync.RWMutex
	rejected map[string]int64 // cell id -> count, for one aggregated log line
}

// New builds a Router for the given local cell. A nil directory is valid and
// means "no cell is known other than this one": foreign requests are then
// refused without an address, which is still better than serving them.
func New(localCell string, directory CellDirectory, logger *zap.Logger) *Router {
	return &Router{
		localCell: localCell,
		directory: directory,
		logger:    logger,
		rejected:  make(map[string]int64),
	}
}

// Decision is the outcome of one routing check.
type Decision struct {
	// Local reports whether this cell may serve the request.
	Local bool
	// CellID is the cell the request belongs to (empty when the token carries
	// no placement).
	CellID string
	// Endpoint is where the client should retry, when known.
	Endpoint string
}

// Check decides whether this cell serves a request from the given token cell.
//
// An empty tokenCell is local: see the package docstring.
func (r *Router) Check(tokenCell string) Decision {
	cell := strings.TrimSpace(tokenCell)
	if cell == "" || cell == r.localCell {
		return Decision{Local: true, CellID: cell}
	}
	endpoint := ""
	if r.directory != nil {
		if resolved, ok := r.directory.Endpoint(cell); ok {
			endpoint = strings.TrimSuffix(resolved, "/")
		}
	}
	r.noteRejected(cell)
	return Decision{Local: false, CellID: cell, Endpoint: endpoint}
}

// cellMaintenanceRetryAfter is advertised with CELL_MAINTENANCE. Maintenance is
// an operator-driven state that ends in minutes to hours; a shorter window
// would invite a retry loop against a verdict that cannot have changed.
const cellMaintenanceRetryAfter = "60"

// MaintenanceProbe is the optional extension a CellDirectory may satisfy to
// expose per-cell registry status.
//
// Optional by design: the routing decision must not depend on the global tier
// being reachable, so a directory that cannot answer (static map, test double,
// registry down) simply never triggers the maintenance refusal.
type MaintenanceProbe interface {
	Status(cellID string) (string, bool)
}

// Middleware refuses requests belonging to another cell, and answers
// CELL_MAINTENANCE when this cell is marked as under maintenance in the
// registry (design 08 §7).
//
// Placed AFTER authentication (it reads the verified token's claim) and BEFORE
// authorization: a request this cell must not serve should not consume an
// authorization lookup, and its permissions here are meaningless anyway — the
// tenant's data lives elsewhere.
func (r *Router) Middleware(cellFromRequest func(*http.Request) string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			decision := r.Check(cellFromRequest(req))
			if decision.Local {
				if r.inMaintenance() {
					r.writeMaintenance(w, req)
					return
				}
				next.ServeHTTP(w, req)
				return
			}
			r.writeMisdirected(w, req, decision)
		})
	}
}

// inMaintenance reports whether the LOCAL cell is explicitly marked
// "maintenance" in the registry.
//
// Only the explicit "maintenance" status refuses traffic: the registry's other
// non-active values (a drained cell mid-decommission, say) still own tenants
// that must keep working — refusing them would deadlock the very drain the
// status describes. Unknown (unregistered, unreachable registry) also serves,
// per the package's availability rule.
func (r *Router) inMaintenance() bool {
	probe, ok := r.directory.(MaintenanceProbe)
	if !ok {
		return false
	}
	status, known := probe.Status(r.localCell)
	return known && status == "maintenance"
}

// writeMaintenance answers 503 CELL_MAINTENANCE with a Retry-After window.
//
// The contract's purpose: during a maintenance window the client should wait
// and transparently retry (the frontend interceptor does exactly this for
// Retry-After responses), not surface an error the user cannot act on.
func (r *Router) writeMaintenance(w http.ResponseWriter, req *http.Request) {
	r.noteMaintenance()

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Retry-After", cellMaintenanceRetryAfter)
	w.WriteHeader(http.StatusServiceUnavailable)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error_code":  "CELL_MAINTENANCE",
		"message":     "This cell is under maintenance; retry after the advertised window",
		"cell_id":     r.localCell,
	})
}

// noteMaintenance counts maintenance refusals, log-sparsely like rejections: a
// maintenance window refused on every request of every tenant would otherwise
// bury the log.
func (r *Router) noteMaintenance() {
	r.mu.Lock()
	r.rejected["maintenance"]++
	count := r.rejected["maintenance"]
	r.mu.Unlock()

	if count == 1 || count%1000 == 0 {
		r.logger.Warn("cell_in_maintenance",
			zap.String("local_cell", r.localCell),
			zap.Int64("refused", count),
			zap.String("hint", "the registry marks this cell 'maintenance'; "+
				"clear the status to resume serving"))
	}
}

// writeMisdirected answers 421 with the address the client should use.
func (r *Router) writeMisdirected(
	w http.ResponseWriter, req *http.Request, decision Decision,
) {
	body := map[string]any{
		"error":      "this cell does not serve your tenant",
		"error_code": "WRONG_CELL",
		"cell_id":    decision.CellID,
	}
	if decision.Endpoint != "" {
		body["endpoint"] = decision.Endpoint
		// Location lets a plain HTTP client follow the redirect semantics it
		// already understands, without parsing our body.
		w.Header().Set("Location", decision.Endpoint+req.URL.RequestURI())
	} else {
		// Known-foreign but unresolvable: say so explicitly rather than sending
		// the client somewhere arbitrary.
		body["message"] = "the cell serving your tenant is not registered; " +
			"contact the operator"
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Datacode-Cell", r.localCell)
	w.WriteHeader(http.StatusMisdirectedRequest)
	_ = json.NewEncoder(w).Encode(body)
}

// noteRejected counts a rejection per foreign cell.
//
// Counted rather than logged per request: a misconfigured global edge sends a
// steady stream of these, and one line per request would bury everything else.
// RejectionCounts is read by /health so the condition is observable.
func (r *Router) noteRejected(cellID string) {
	r.mu.Lock()
	r.rejected[cellID]++
	count := r.rejected[cellID]
	r.mu.Unlock()

	// Log the first one, then powers of ten: enough to notice, bounded in volume.
	if count == 1 || count%1000 == 0 {
		r.logger.Warn("request_for_foreign_cell",
			zap.String("local_cell", r.localCell),
			zap.String("token_cell", cellID),
			zap.Int64("count", count),
			zap.String("hint", "the global edge is routing this tenant to the "+
				"wrong cell, or its token predates a migration"))
	}
}

// RejectionCounts returns a copy of the per-cell rejection counters.
func (r *Router) RejectionCounts() map[string]int64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make(map[string]int64, len(r.rejected))
	for cell, count := range r.rejected {
		out[cell] = count
	}
	return out
}

// LocalCell returns the cell this gateway belongs to.
func (r *Router) LocalCell() string { return r.localCell }
