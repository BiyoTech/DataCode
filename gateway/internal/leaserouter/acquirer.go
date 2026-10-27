package leaserouter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"go.uber.org/zap"

	"github.com/datacode/shared/lease"
)

// AcquireTimeout bounds one placement round trip.
//
// The runtime's accept handshake is sub-second (SET NX + flock; materialization
// continues in the background), so a timeout here means the control path is
// broken, not that a repository is large. It must stay well under the gateway's
// overall request budget so the request can still degrade to the stable-hash
// path instead of dying on the fence.
const AcquireTimeout = 5 * time.Second

// LeaseUnavailableError is the runtime's 429 LEASE_UNAVAILABLE verdict: the
// fleet has no lease capacity for this working copy right now (design 08 §7).
//
// Typed, not a bare error, because the router must treat it DIFFERENTLY from
// every other acquire failure: a transport hiccup or a bug justifies degrading
// to the stable-hash path, but a capacity verdict is exactly what the stable
// hash would route around — degrading here would hand the request to an
// instance the placement service just refused, defeating capacity governance.
type LeaseUnavailableError struct {
	// RetryAfter is the window the runtime advertised, "" when absent.
	RetryAfter string
}

func (e *LeaseUnavailableError) Error() string {
	return "leaserouter: no lease capacity available (LEASE_UNAVAILABLE)"
}

// HTTPStatus, ErrorCode and RetryAfter implement handler.StatusError, so the
// proxy layer can answer with the contract's 429 + Retry-After instead of the
// generic 503 it writes for unresolvable routes.
func (e *LeaseUnavailableError) HTTPStatus() int         { return http.StatusTooManyRequests }
func (e *LeaseUnavailableError) ErrorCode() string       { return "LEASE_UNAVAILABLE" }
func (e *LeaseUnavailableError) RetryAfterValue() string { return e.RetryAfter }

// ErrorBody implements the optional richer payload of handler.StatusError.
func (e *LeaseUnavailableError) ErrorBody() map[string]any {
	return map[string]any{
		"message": "No runtime capacity is available for this working copy; " +
			"retry after the advertised window",
	}
}

// LeaseSubsystemOffError is the runtime's 503: the lease subsystem is not
// running on the fleet at all (`lease.enabled` false, or no internal token).
//
// Typed for the same reason as the capacity verdict, but with the opposite
// consequence. It is not a failure to be retried: it is a STANDING deployment
// state that cannot change between two requests. Retrying it once per request
// costs a round trip on the hot path of every file read and buries the log in
// warnings that describe the configuration rather than a fault — which is what
// a single-replica dev stack (leases off by default) used to produce
// continuously. The router remembers it instead and re-probes on a timer.
type LeaseSubsystemOffError struct{}

func (e *LeaseSubsystemOffError) Error() string {
	return "leaserouter: the runtime fleet is not running the lease subsystem"
}

// HTTPAcquirer asks the runtime fleet to place a lease.
//
// The request is load balanced to ANY runtime replica (the runtime's placement
// service picks the winning instance from there — see src/lease/server.py). One
// shared http.Client: acquires happen on lease misses only, and a client per
// call would rebuild connection pools on the request path.
type HTTPAcquirer struct {
	baseURL string
	token   string
	client  *http.Client
	logger  *zap.Logger
}

// NewHTTPAcquirer builds an acquirer against the runtime service address.
// An empty baseURL yields a disabled acquirer (Enabled() false), which keeps
// lease misses on the stable-hash path — the pre-placement behaviour.
func NewHTTPAcquirer(baseURL, internalToken string, logger *zap.Logger) *HTTPAcquirer {
	return &HTTPAcquirer{
		baseURL: baseURL,
		token:   internalToken,
		client: &http.Client{
			Timeout: AcquireTimeout,
			// Transport defaults: the acquire path is rare enough that a
			// custom tuning would be speculative.
		},
		logger: logger,
	}
}

// Enabled reports whether placement can be attempted at all.
func (a *HTTPAcquirer) Enabled() bool {
	return a != nil && a.baseURL != ""
}

// Acquire asks for a placement and returns the record to route on.
//
// Errors are returned, never swallowed: the router's degradation decision
// (stable hash vs 503) depends on telling "placement said no" from "placement
// unreachable", and a swallowed error would silently disable placement.
func (a *HTTPAcquirer) Acquire(ctx context.Context, key lease.Key) (lease.Record, error) {
	if !a.Enabled() {
		return lease.Record{}, fmt.Errorf("leaserouter: acquirer not configured")
	}

	payload, err := json.Marshal(map[string]string{"lease_key": key.String()})
	if err != nil {
		return lease.Record{}, fmt.Errorf("leaserouter: encode acquire request: %w", err)
	}

	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost,
		a.baseURL+"/internal/leases/acquire",
		bytes.NewReader(payload),
	)
	if err != nil {
		return lease.Record{}, fmt.Errorf("leaserouter: build acquire request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if a.token != "" {
		req.Header.Set("X-Internal-Token", a.token)
	}

	resp, err := a.client.Do(req)
	if err != nil {
		return lease.Record{}, fmt.Errorf("leaserouter: acquire request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// 429 is a capacity verdict, not a failure to reach the fleet: it must
		// reach the caller as a typed error so the router propagates it instead
		// of degrading to the stable hash (which would route around the
		// capacity decision the runtime just made).
		if resp.StatusCode == http.StatusTooManyRequests {
			return lease.Record{}, &LeaseUnavailableError{
				RetryAfter: resp.Header.Get("Retry-After"),
			}
		}
		// 503 from the runtime means the fleet could not be asked at all:
		// degrading to the stable hash is correct. Typed, because the reason is
		// a standing deployment state rather than a transient fault — see
		// LeaseSubsystemOffError. 401/403 means the shared secret is wrong,
		// which is a deployment error worth its own line.
		if resp.StatusCode == http.StatusServiceUnavailable {
			return lease.Record{}, &LeaseSubsystemOffError{}
		}
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			a.logger.Error("lease_acquire_unauthorized",
				zap.Int("status", resp.StatusCode),
				zap.String("hint", "RUNTIME_INTERNAL_TOKEN / gateway token mismatch"))
		}
		return lease.Record{}, fmt.Errorf(
			"leaserouter: acquire returned status %d", resp.StatusCode)
	}

	var body struct {
		RuntimeID string `json:"runtime_id"`
		Addr      string `json:"addr"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return lease.Record{}, fmt.Errorf("leaserouter: decode acquire response: %w", err)
	}
	if body.Addr == "" {
		// A success without an address is a contract violation on the runtime
		// side; routing on it would send traffic nowhere.
		return lease.Record{}, fmt.Errorf(
			"leaserouter: acquire response for %s has no addr", key.String())
	}
	return lease.Record{
		RuntimeID:   body.RuntimeID,
		RuntimeAddr: body.Addr,
	}, nil
}
