// Package handler — declarative route configuration for the Dagster Service.
//
// The Dagster Service exposes endpoints under the identity path
// "/api/v1/dagster/*", so the gateway forwards without prefix rewriting
// (same strategy as the Ontology Service).
//
// Only GET endpoints that require workspace isolation are declared here as
// validated routes; every other Dagster route (POST run/reload/ensure, run
// status/logs/cancel, health, mode) is transparently forwarded by the
// catch-all GenericProxy registered in router.go.
package handler

// DagsterValidatedRoutes defines validated GET routes under the /dagster route
// group. Patterns are relative to the "/dagster" chi sub-router. These require
// workspace_path for per-workspace scoping, mirroring the SQLMesh lineage and
// Ontology endpoints.
var DagsterValidatedRoutes = []ValidatedRoute{
	{Method: "GET", Pattern: "/assets/graph", Required: []string{"workspace_path"}},
	{Method: "GET", Pattern: "/schedules", Required: []string{"workspace_path"}},
}
