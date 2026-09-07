package cellrouter

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.uber.org/zap"
)

type staticDirectory map[string]string

func (d staticDirectory) Endpoint(cellID string) (string, bool) {
	endpoint, ok := d[cellID]
	return endpoint, ok
}

// statusDirectory additionally answers the MaintenanceProbe, so the router's
// maintenance branch can be exercised without a live identity-service.
type statusDirectory struct {
	staticDirectory
	statuses map[string]string
}

func (d statusDirectory) Status(cellID string) (string, bool) {
	status, ok := d.statuses[cellID]
	return status, ok
}

func testRouter(directory CellDirectory) *Router {
	return New("cell-0", directory, zap.NewNop())
}

// A tenant of this cell is served here — the ordinary path, and it must not
// consult the directory at all: local traffic depending on the global registry
// is the coupling cells exist to remove.
func TestLocalCellIsServed(t *testing.T) {
	router := testRouter(nil)

	decision := router.Check("cell-0")

	if !decision.Local {
		t.Fatalf("a request for the local cell must be served locally")
	}
}

// Tokens minted before placement existed carry no cell. Rejecting them would
// log out every existing session on upgrade, and a single-cell install is
// exactly the case where every tenant is local.
func TestMissingCellClaimIsServedLocally(t *testing.T) {
	router := testRouter(nil)

	for _, claim := range []string{"", "   "} {
		if !router.Check(claim).Local {
			t.Fatalf("an empty cell claim (%q) must be served locally", claim)
		}
	}
}

func TestForeignCellIsRefusedWithItsAddress(t *testing.T) {
	router := testRouter(staticDirectory{"cell-1": "https://cell-1.example/"})

	decision := router.Check("cell-1")

	if decision.Local {
		t.Fatal("a request for another cell must not be served here")
	}
	if decision.CellID != "cell-1" {
		t.Fatalf("cell id = %q, want cell-1", decision.CellID)
	}
	// Trailing slash trimmed so the address can be concatenated with a path.
	if decision.Endpoint != "https://cell-1.example" {
		t.Fatalf("endpoint = %q, want https://cell-1.example", decision.Endpoint)
	}
}

// A foreign cell that is not registered must be refused WITHOUT an address:
// sending the client somewhere arbitrary would show them an empty platform,
// which is worse than an error.
func TestUnknownForeignCellIsRefusedWithoutAddress(t *testing.T) {
	router := testRouter(staticDirectory{})

	decision := router.Check("cell-9")

	if decision.Local {
		t.Fatal("an unknown cell must not be served locally")
	}
	if decision.Endpoint != "" {
		t.Fatalf("endpoint = %q, want empty for an unregistered cell", decision.Endpoint)
	}
}

// With no directory at all the router still refuses foreign requests. Serving
// them would be the actual failure; not naming the address is a degradation.
func TestNilDirectoryStillRefusesForeignCells(t *testing.T) {
	router := testRouter(nil)

	if router.Check("cell-1").Local {
		t.Fatal("a foreign cell must be refused even without a directory")
	}
}

func TestMiddlewareServesLocalRequests(t *testing.T) {
	router := testRouter(nil)
	served := false
	handler := router.Middleware(func(*http.Request) string { return "cell-0" })(
		http.HandlerFunc(func(http.ResponseWriter, *http.Request) { served = true }),
	)

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/api/v1/workspaces", nil))

	if !served {
		t.Fatal("the local request never reached the handler")
	}
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", recorder.Code)
	}
}

// 421 rather than a proxy: forwarding would make this cell's availability
// depend on the other cell's, and would hide the misrouting.
func TestMiddlewareAnswers421ForForeignCells(t *testing.T) {
	router := testRouter(staticDirectory{"cell-1": "https://cell-1.example"})
	served := false
	handler := router.Middleware(func(*http.Request) string { return "cell-1" })(
		http.HandlerFunc(func(http.ResponseWriter, *http.Request) { served = true }),
	)

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder,
		httptest.NewRequest(http.MethodGet, "/api/v1/workspaces?page=2", nil))

	if served {
		t.Fatal("a foreign request must not reach the handler")
	}
	if recorder.Code != http.StatusMisdirectedRequest {
		t.Fatalf("status = %d, want 421", recorder.Code)
	}

	var body map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}
	if body["error_code"] != "WRONG_CELL" {
		t.Fatalf("error_code = %v, want WRONG_CELL", body["error_code"])
	}
	if body["endpoint"] != "https://cell-1.example" {
		t.Fatalf("endpoint = %v", body["endpoint"])
	}
	// Location carries the full URL so a plain client can follow it, query
	// string included — dropping the query would silently change the request.
	location := recorder.Header().Get("Location")
	if !strings.HasSuffix(location, "/api/v1/workspaces?page=2") {
		t.Fatalf("Location = %q, want the original request URI appended", location)
	}
	if recorder.Header().Get("X-Datacode-Cell") != "cell-0" {
		t.Fatal("the response must name the cell that refused it")
	}
}

func TestMiddlewareExplainsAnUnregisteredCell(t *testing.T) {
	router := testRouter(staticDirectory{})
	handler := router.Middleware(func(*http.Request) string { return "cell-9" })(
		http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}),
	)

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/api/v1/x", nil))

	var body map[string]any
	_ = json.Unmarshal(recorder.Body.Bytes(), &body)
	if _, hasEndpoint := body["endpoint"]; hasEndpoint {
		t.Fatal("an unregistered cell must not be given an address")
	}
	if body["message"] == nil {
		t.Fatal("the response must explain why no address is offered")
	}
	if recorder.Header().Get("Location") != "" {
		t.Fatal("no Location may be set when the address is unknown")
	}
}

// A misconfigured global edge produces a steady stream of these; the counter is
// what makes the condition observable without one log line per request.
func TestRejectionsAreCounted(t *testing.T) {
	router := testRouter(staticDirectory{"cell-1": "https://cell-1.example"})

	router.Check("cell-1")
	router.Check("cell-1")
	router.Check("cell-2")
	router.Check("cell-0") // local: not counted

	counts := router.RejectionCounts()
	if counts["cell-1"] != 2 {
		t.Fatalf("cell-1 count = %d, want 2", counts["cell-1"])
	}
	if counts["cell-2"] != 1 {
		t.Fatalf("cell-2 count = %d, want 1", counts["cell-2"])
	}
	if _, counted := counts["cell-0"]; counted {
		t.Fatal("a locally served request must not be counted as a rejection")
	}
}

// ── directory ──────────────────────────────────────────────────────────

func TestRegistryDirectoryReadsTheGlobalRegistry(t *testing.T) {
	var presentedToken string
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			presentedToken = r.Header.Get("X-Internal-Token")
			_, _ = w.Write([]byte(`{"cells":[
				{"cell_id":"cell-0","endpoint":"https://a.example","status":"active"},
				{"cell_id":"cell-1","endpoint":"https://b.example","status":"active"}
			]}`))
		}))
	defer server.Close()

	directory := NewRegistryDirectory(
		server.URL, "secret", time.Minute, time.Second, zap.NewNop())

	endpoint, ok := directory.Endpoint("cell-1")
	if !ok || endpoint != "https://b.example" {
		t.Fatalf("endpoint = %q ok = %v", endpoint, ok)
	}
	if presentedToken != "secret" {
		t.Fatalf("the internal token was not presented: %q", presentedToken)
	}
}

// An unreachable registry must keep serving the previous contents: a cell's
// address changes rarely, so yesterday's answer beats none — and this value only
// redirects a client already known to be in the wrong place.
func TestRegistryDirectoryKeepsCacheWhenRefreshFails(t *testing.T) {
	failing := true
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) {
			if failing {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			_, _ = w.Write([]byte(
				`{"cells":[{"cell_id":"cell-1","endpoint":"https://b.example"}]}`))
		}))
	defer server.Close()

	directory := NewRegistryDirectory(
		server.URL, "secret", time.Nanosecond, time.Second, zap.NewNop())

	failing = false
	if endpoint, _ := directory.Endpoint("cell-1"); endpoint != "https://b.example" {
		t.Fatalf("warm-up failed: %q", endpoint)
	}

	failing = true
	if endpoint, _ := directory.Endpoint("cell-1"); endpoint != "https://b.example" {
		t.Fatalf("a failed refresh discarded the cache: %q", endpoint)
	}
}

func TestRegistryDirectoryIsDisabledWithoutCredentials(t *testing.T) {
	for _, directory := range []*RegistryDirectory{
		NewRegistryDirectory("", "secret", time.Minute, time.Second, zap.NewNop()),
		NewRegistryDirectory("http://x", "", time.Minute, time.Second, zap.NewNop()),
	} {
		if directory.Enabled() {
			t.Fatal("a directory without URL or token must report disabled")
		}
		if _, ok := directory.Endpoint("cell-1"); ok {
			t.Fatal("a disabled directory must not resolve anything")
		}
	}
}

// An entry without a cell id is skipped rather than stored under "": an empty
// key would collide with "the token carried no placement", which is served
// locally.
func TestRegistryDirectorySkipsEntriesWithoutAnID(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"cells":[{"cell_id":"","endpoint":"https://x"}]}`))
		}))
	defer server.Close()

	directory := NewRegistryDirectory(
		server.URL, "secret", time.Minute, time.Second, zap.NewNop())
	directory.Endpoint("cell-1")

	if len(directory.Cells()) != 0 {
		t.Fatalf("an id-less entry was stored: %+v", directory.Cells())
	}
}

// ── CELL_MAINTENANCE (design 08 §7) ───────────────────────────────────────

// serveCellMiddleware drives one local request through the middleware and
// returns the status and body the client received.
func serveCellMiddleware(router *Router) (int, string) {
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/v1/workspaces", nil)
	router.Middleware(func(*http.Request) string { return "cell-0" })(next).ServeHTTP(
		recorder, request)
	return recorder.Code, recorder.Body.String()
}

// TestMaintenanceCellAnswers503 pins the contract: a cell the registry marks
// "maintenance" refuses local traffic with CELL_MAINTENANCE + Retry-After, so
// the client waits out the window instead of surfacing an error.
func TestMaintenanceCellAnswers503(t *testing.T) {
	directory := statusDirectory{
		staticDirectory: staticDirectory{"cell-0": "https://cell-0.example/"},
		statuses:        map[string]string{"cell-0": "maintenance"},
	}
	router := testRouter(directory)

	status, body := serveCellMiddleware(router)

	if status != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", status)
	}
	if !strings.Contains(body, "CELL_MAINTENANCE") {
		t.Errorf("body = %q, want the contract error code", body)
	}
	if !strings.Contains(body, "cell-0") {
		t.Errorf("body = %q, want the cell id for diagnostics", body)
	}
}

// TestActiveCellServesLocally: "active" is the ordinary registry status and
// must not trigger the refusal.
func TestActiveCellServesLocally(t *testing.T) {
	directory := statusDirectory{
		staticDirectory: staticDirectory{"cell-0": "https://cell-0.example/"},
		statuses:        map[string]string{"cell-0": "active"},
	}
	router := testRouter(directory)

	status, _ := serveCellMiddleware(router)

	if status != http.StatusOK {
		t.Fatalf("status = %d, want 200 (active cell serves)", status)
	}
}

// TestUnknownStatusServesLocally pins the availability rule: a cell the
// registry does not know (unregistered, or the registry is unreachable) keeps
// serving — local availability must not depend on the global tier.
func TestUnknownStatusServesLocally(t *testing.T) {
	// A directory without Status(): not a MaintenanceProbe at all.
	router := testRouter(staticDirectory{"cell-0": "https://cell-0.example/"})

	status, _ := serveCellMiddleware(router)

	if status != http.StatusOK {
		t.Fatalf("status = %d, want 200 (no probe, no refusal)", status)
	}
}

// TestNonMaintenanceStatusServesLocally: the registry's other non-active
// values (drained mid-decommission, provisioning) still own tenants that must
// keep working — only the explicit "maintenance" refusal is safe.
func TestNonMaintenanceStatusServesLocally(t *testing.T) {
	directory := statusDirectory{
		staticDirectory: staticDirectory{"cell-0": "https://cell-0.example/"},
		statuses:        map[string]string{"cell-0": "draining"},
	}
	router := testRouter(directory)

	status, _ := serveCellMiddleware(router)

	if status != http.StatusOK {
		t.Fatalf("status = %d, want 200 (non-maintenance status serves)", status)
	}
}
