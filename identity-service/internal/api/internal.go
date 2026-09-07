// Package api — internal service-to-service endpoints.
//
// These carry INFRASTRUCTURE facts between platform services rather than user
// traffic: the authorization snapshot the gateway decides with, and the
// tenant -> cell routing table the global edge needs. They are guarded by the
// shared internal token and, per platform convention, FAIL CLOSED: with no
// token configured the endpoints are disabled entirely rather than served
// unauthenticated (an open authz endpoint is a map of who can do what).
//
// Note the paths are NOT under /api/v1, so a gateway never forwards them and
// they are unreachable from the user side.
package api

import (
	"crypto/subtle"
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"go.uber.org/zap"

	"github.com/datacode/identity-service/internal/authz"
	"github.com/datacode/identity-service/internal/placement"
	"github.com/datacode/shared/httperr"
)

// InternalHandler serves the service-to-service API.
type InternalHandler struct {
	authz     *authz.Service
	placement *placement.Store
	// bindings serves the authorization BINDING MUTATIONS the gateway used to
	// perform against its own tables. Mounted here so it inherits this
	// handler's token guard and fail-closed behaviour.
	bindings *BindingHandler
	token    string
	logger   *zap.Logger
}

func NewInternalHandler(
	authzService *authz.Service, placementStore *placement.Store,
	bindingHandler *BindingHandler, token string, logger *zap.Logger,
) *InternalHandler {
	return &InternalHandler{
		authz:     authzService,
		placement: placementStore,
		bindings:  bindingHandler,
		token:     token,
		logger:    logger,
	}
}

// Enabled reports whether the internal API may serve requests.
func (h *InternalHandler) Enabled() bool { return h.token != "" }

// Routes mounts the internal API, or a 404-everything router when no token is
// configured.
func (h *InternalHandler) Routes() chi.Router {
	r := chi.NewRouter()
	if !h.Enabled() {
		h.logger.Warn("internal_api_disabled",
			zap.String("reason", "IDENTITY_SERVICE_INTERNAL_TOKEN is not set"),
			zap.String("effect", "authz/resolve and placement are unavailable; "+
				"the gateway will fall back to deny-by-default"))
		r.HandleFunc("/*", func(w http.ResponseWriter, _ *http.Request) {
			http.Error(w, `{"error":"internal api disabled"}`, http.StatusNotFound)
		})
		return r
	}

	r.Use(h.requireToken)
	r.Get("/authz/resolve", h.ResolveAuthz)
	r.Get("/tenants/{tenantID}/placement", h.GetPlacement)
	r.Post("/tenants/{tenantID}/placement", h.SetPlacement)
	r.Get("/cells", h.ListCells)
	r.Post("/cells", h.RegisterCell)
	if h.bindings != nil {
		h.bindings.Routes(r)
	}
	return r
}

// requireToken authenticates the caller with a constant-time comparison.
func (h *InternalHandler) requireToken(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		presented := r.Header.Get("X-Internal-Token")
		if presented == "" {
			// Accept the bearer form as well: some infrastructure clients can
			// only set Authorization. The scheme match is case-insensitive, so
			// a client sending "bearer" is not silently unauthenticated.
			presented = httperr.BearerToken(r.Header.Get("Authorization"))
		}
		if subtle.ConstantTimeCompare([]byte(presented), []byte(h.token)) != 1 {
			http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// ResolveAuthz returns the authorization snapshot for one (user, tenant).
//
// GET /internal/authz/resolve?user_id=...&tenant_id=...
func (h *InternalHandler) ResolveAuthz(w http.ResponseWriter, r *http.Request) {
	userID := r.URL.Query().Get("user_id")
	tenantID := r.URL.Query().Get("tenant_id")
	if userID == "" || tenantID == "" {
		http.Error(w, `{"error":"user_id and tenant_id are required"}`, http.StatusBadRequest)
		return
	}

	snapshot, err := h.authz.Resolve(r.Context(), userID, tenantID)
	if err != nil {
		h.logger.Error("authz_resolve_failed",
			zap.String("user_id", userID), zap.String("tenant_id", tenantID), zap.Error(err))
		http.Error(w, `{"error":"resolve failed"}`, http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, snapshot)
}

// GetPlacement returns a tenant's cell assignment.
func (h *InternalHandler) GetPlacement(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")
	p, err := h.placement.Get(r.Context(), tenantID)
	if err != nil {
		h.logger.Error("placement_read_failed",
			zap.String("tenant_id", tenantID), zap.Error(err))
		http.Error(w, `{"error":"placement read failed"}`, http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, p)
}

// SetPlacement assigns a cell or advances the migration state.
//
// Body: {"cell_id": "...} to assign, or
//
//	{"migration_state": "...", "target_cell_id": "..."} to stage a move.
func (h *InternalHandler) SetPlacement(w http.ResponseWriter, r *http.Request) {
	tenantID := chi.URLParam(r, "tenantID")

	var body struct {
		CellID         string `json:"cell_id"`
		MigrationState string `json:"migration_state"`
		TargetCellID   string `json:"target_cell_id"`
	}
	if err := json.NewDecoder(
		http.MaxBytesReader(w, r.Body, httperr.MaxRequestBodyBytes),
	).Decode(&body); err != nil {
		http.Error(w, `{"error":"invalid body"}`, http.StatusBadRequest)
		return
	}

	ctx := r.Context()
	var err error
	switch {
	case body.MigrationState != "":
		err = h.placement.SetMigrationState(ctx, tenantID, body.MigrationState, body.TargetCellID)
	case body.CellID != "":
		err = h.placement.Assign(ctx, tenantID, body.CellID)
	default:
		http.Error(w, `{"error":"cell_id or migration_state is required"}`, http.StatusBadRequest)
		return
	}
	if err != nil {
		h.logger.Error("placement_write_failed",
			zap.String("tenant_id", tenantID), zap.Error(err))
		http.Error(w, `{"error":"placement write failed"}`, http.StatusInternalServerError)
		return
	}

	p, readErr := h.placement.Get(ctx, tenantID)
	if readErr != nil {
		http.Error(w, `{"error":"placement read-back failed"}`, http.StatusInternalServerError)
		return
	}
	h.logger.Info("placement_updated",
		zap.String("tenant_id", tenantID),
		zap.String("cell_id", p.CellID),
		zap.String("migration_state", p.MigrationState))
	writeJSON(w, http.StatusOK, p)
}

// ListCells returns the cell inventory with load counters.
func (h *InternalHandler) ListCells(w http.ResponseWriter, r *http.Request) {
	cells, err := h.placement.ListCells(r.Context())
	if err != nil {
		http.Error(w, `{"error":"cell list failed"}`, http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"cells": cells})
}

// RegisterCell records a cell's routable endpoint and capacity.
//
// POST /internal/cells
// Body: {"cell_id","region","endpoint","status","capacity_tenants"}
//
// Called by each cell's own control plane at startup, so the endpoint the global
// edge dials is written by the party that knows it. `assigned_tenants` is not
// accepted: it is derived from the placement table, and letting a cell report
// its own tenant count would let a stale value distort placement decisions.
func (h *InternalHandler) RegisterCell(w http.ResponseWriter, r *http.Request) {
	var body struct {
		CellID          string `json:"cell_id"`
		Region          string `json:"region"`
		Endpoint        string `json:"endpoint"`
		Status          string `json:"status"`
		CapacityTenants int    `json:"capacity_tenants"`
	}
	if err := json.NewDecoder(
		http.MaxBytesReader(w, r.Body, httperr.MaxRequestBodyBytes),
	).Decode(&body); err != nil {
		http.Error(w, `{"error":"invalid body"}`, http.StatusBadRequest)
		return
	}
	if body.CellID == "" {
		http.Error(w, `{"error":"cell_id is required"}`, http.StatusBadRequest)
		return
	}
	if body.Endpoint == "" {
		// An endpoint-less registration would satisfy the schema and break
		// routing: the edge would resolve the cell and have nowhere to send it.
		http.Error(w, `{"error":"endpoint is required: the global edge dials it"}`,
			http.StatusBadRequest)
		return
	}

	if err := h.placement.RegisterCell(r.Context(), placement.Cell{
		CellID:          body.CellID,
		Region:          body.Region,
		Endpoint:        body.Endpoint,
		Status:          body.Status,
		CapacityTenants: body.CapacityTenants,
	}); err != nil {
		h.logger.Error("cell_register_failed",
			zap.String("cell_id", body.CellID), zap.Error(err))
		http.Error(w, `{"error":"cell registration failed"}`, http.StatusInternalServerError)
		return
	}

	cell, found, err := h.placement.GetCell(r.Context(), body.CellID)
	if err != nil || !found {
		http.Error(w, `{"error":"cell read-back failed"}`, http.StatusInternalServerError)
		return
	}
	h.logger.Info("cell_registered",
		zap.String("cell_id", cell.CellID),
		zap.String("endpoint", cell.Endpoint),
		zap.String("status", cell.Status),
		zap.Int("capacity_tenants", cell.CapacityTenants))
	writeJSON(w, http.StatusOK, cell)
}
