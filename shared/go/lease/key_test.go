package lease

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// vectors mirrors shared/lease-vectors.json, which the Python tests read too.
type vectors struct {
	ValidKeys []struct {
		TenantID    string `json:"tenant_id"`
		WorkspaceID string `json:"workspace_id"`
		UserID      string `json:"user_id"`
		Environment string `json:"environment"`
		Key         string `json:"key"`
	} `json:"valid_keys"`
	InvalidKeys []struct {
		Key string `json:"key"`
		Why string `json:"why"`
	} `json:"invalid_keys"`
	WorkingCopyPaths []struct {
		Path string `json:"path"`
		Key  string `json:"key"`
	} `json:"working_copy_paths"`
	InvalidWorkingCopyPaths []struct {
		Path string `json:"path"`
		Why  string `json:"why"`
	} `json:"invalid_working_copy_paths"`
	NormalizeAddr []struct {
		Addr     string `json:"addr"`
		Expected string `json:"expected"`
		Why      string `json:"why"`
	} `json:"normalize_addr"`
	StableHash []struct {
		Key   string `json:"key"`
		Count int    `json:"count"`
		Index int    `json:"index"`
	} `json:"stable_hash"`
}

func loadVectors(t *testing.T) vectors {
	t.Helper()
	// ../../lease-vectors.json relative to shared/go/lease.
	raw, err := os.ReadFile(filepath.Join("..", "..", "lease-vectors.json"))
	if err != nil {
		t.Fatalf("cannot read shared vectors: %v", err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("cannot decode shared vectors: %v", err)
	}
	if len(v.ValidKeys) == 0 || len(v.InvalidKeys) == 0 {
		t.Fatal("shared vectors are empty; the cross-language check is vacuous")
	}
	return v
}

// TestKeyRoundTripMatchesSharedVectors is the cross-language guarantee: Go must
// render and parse exactly the keys Python does. A divergence here would not
// raise anywhere at runtime — it would silently double-mount a working copy.
func TestKeyRoundTripMatchesSharedVectors(t *testing.T) {
	v := loadVectors(t)

	for _, tc := range v.ValidKeys {
		key, err := NewKey(tc.TenantID, tc.WorkspaceID, tc.UserID, tc.Environment)
		if err != nil {
			t.Errorf("NewKey(%q) unexpected error: %v", tc.Key, err)
			continue
		}
		if got := key.String(); got != tc.Key {
			t.Errorf("String() = %q, shared vector says %q", got, tc.Key)
		}

		parsed, err := ParseKey(tc.Key)
		if err != nil {
			t.Errorf("ParseKey(%q) unexpected error: %v", tc.Key, err)
			continue
		}
		if parsed != key {
			t.Errorf("ParseKey(%q) = %+v, want %+v", tc.Key, parsed, key)
		}
	}
}

func TestMalformedKeysAreRejected(t *testing.T) {
	v := loadVectors(t)

	for _, tc := range v.InvalidKeys {
		// Coercing any of these into a valid key would let two distinct working
		// copies share one lease.
		if _, err := ParseKey(tc.Key); err == nil {
			t.Errorf("ParseKey(%q) succeeded but should fail: %s", tc.Key, tc.Why)
		}
	}
}

func TestKeyFromWorkingCopyPathMatchesSharedVectors(t *testing.T) {
	v := loadVectors(t)

	for _, tc := range v.WorkingCopyPaths {
		key, err := KeyFromWorkingCopyPath(tc.Path)
		if err != nil {
			t.Errorf("KeyFromWorkingCopyPath(%q) unexpected error: %v", tc.Path, err)
			continue
		}
		if got := key.String(); got != tc.Key {
			t.Errorf("path %q -> %q, shared vector says %q", tc.Path, got, tc.Key)
		}
	}

	for _, tc := range v.InvalidWorkingCopyPaths {
		if _, err := KeyFromWorkingCopyPath(tc.Path); err == nil {
			t.Errorf("KeyFromWorkingCopyPath(%q) succeeded but should fail: %s",
				tc.Path, tc.Why)
		}
	}
}

// TestPathTraversalCannotReachAnotherTenant guards the reason paths are cleaned
// before segmentation: the gateway authorizes on the DERIVED tenant, so a
// traversal that changed the effective directory without changing the derived
// key would be a cross-tenant hole.
func TestPathTraversalCannotReachAnotherTenant(t *testing.T) {
	key, err := KeyFromWorkingCopyPath(
		"/base/t1/ws-t1-shop/u1/envs/../../../t2/ws-t2-x/u2/envs/dev")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// The effective path is t2's, and the derived key must say so rather than
	// reporting the t1 prefix that appears earlier in the string.
	if key.TenantID != "t2" {
		t.Errorf("derived tenant = %q, want %q (path was not normalized)",
			key.TenantID, "t2")
	}
}

func TestBackslashSeparatorsAreNormalized(t *testing.T) {
	key, err := KeyFromWorkingCopyPath(`C:\data\t1\ws-t1-shop\u1\envs\dev`)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got, want := key.String(), "t1/ws-t1-shop/u1/dev"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestEnvNameValidationMirrorsPython(t *testing.T) {
	valid := []string{"dev", "prod", "staging", "feature_1", "a_b_c"}
	for _, name := range valid {
		if !IsValidEnvName(name) {
			t.Errorf("IsValidEnvName(%q) = false, want true", name)
		}
	}
	// "envs" is reserved because it names the directory holding the worktrees.
	invalid := []string{"", "d", "Dev", "1dev", "dev-x", "dev.x", "envs", "_dev"}
	for _, name := range invalid {
		if IsValidEnvName(name) {
			t.Errorf("IsValidEnvName(%q) = true, want false", name)
		}
	}
}

func TestStateRoutability(t *testing.T) {
	if !StateHeld.IsRoutable() {
		t.Error("held leases must be routable")
	}
	// A draining holder still owns the files: routing elsewhere creates a
	// second writer, refusing to route aborts the in-flight operation.
	if !StateDraining.IsRoutable() {
		t.Error("draining leases must stay routable")
	}
	if StateReleased.IsRoutable() {
		t.Error("released leases must not be routable")
	}
}

func TestRecordParsing(t *testing.T) {
	// Exactly the shape src/lease/holder.py writes.
	raw := `{"token":"abc","runtime_id":"workspace-runtime-2",` +
		`"runtime_addr":"10.1.2.3:8002","acquired_at":1780000000.5}`
	record, err := ParseRecord(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if record.RuntimeAddr != "10.1.2.3:8002" || record.RuntimeID != "workspace-runtime-2" {
		t.Errorf("decoded %+v", record)
	}

	// Unknown fields must be ignored so the holder can add diagnostics without
	// a coordinated gateway release.
	if _, err := ParseRecord(
		`{"runtime_addr":"h:1","future_field":123}`); err != nil {
		t.Errorf("unknown fields must be tolerated: %v", err)
	}
}

func TestRecordWithoutAddrIsMalformed(t *testing.T) {
	// Falling back to Service routing here would abandon the single-writer
	// invariant exactly when the lease data is untrustworthy.
	for _, raw := range []string{`{}`, `{"runtime_id":"rt-0"}`, `not json`} {
		if _, err := ParseRecord(raw); err == nil {
			t.Errorf("ParseRecord(%q) succeeded but should fail", raw)
		}
	}
}

func TestRedisKeySegmentsAreStableAndCellAgnostic(t *testing.T) {
	key, err := ParseKey("t1/ws-t1-shop/u1/dev")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	segments := key.RedisKeySegments()
	// The cell/prefix belongs to the Redis client, which is the only component
	// that knows them; embedding them here would produce two sources of truth.
	if len(segments) != 2 || segments[0] != "lease" || segments[1] != key.String() {
		t.Errorf("RedisKeySegments() = %v", segments)
	}
	if got := RuntimeKeySegments("rt-0"); got[0] != "rt" || got[1] != "rt-0" {
		t.Errorf("RuntimeKeySegments() = %v", got)
	}
	if got := RuntimeIndexKeySegments(); got[0] != "rt" || got[1] != "index" {
		t.Errorf("RuntimeIndexKeySegments() = %v", got)
	}
}

func TestNormalizeAddr(t *testing.T) {
	cases := map[string]string{
		"10.1.2.3:8002":         "http://10.1.2.3:8002",
		"http://host:1/":        "http://host:1",
		"https://host:443":      "https://host:443",
		"pod-2.runtime.svc:800": "http://pod-2.runtime.svc:800",
		"":                      "",
	}
	for input, want := range cases {
		if got := NormalizeAddr(input); got != want {
			t.Errorf("NormalizeAddr(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestNormalizeAddrMatchesSharedVectors(t *testing.T) {
	v := loadVectors(t)
	if len(v.NormalizeAddr) == 0 {
		t.Fatal("shared vectors carry no normalize_addr cases")
	}
	for _, tc := range v.NormalizeAddr {
		if got := NormalizeAddr(tc.Addr); got != tc.Expected {
			t.Errorf("NormalizeAddr(%q) = %q, shared vector says %q (%s)",
				tc.Addr, got, tc.Expected, tc.Why)
		}
	}
}

// TestStableHashMatchesSharedVectors is the cross-language guarantee for the
// DEGRADED routing path. Go and Python must land on the same index: if they
// disagree, the gateway and agent-service pick different replicas for one
// working copy during a coordination outage, which is two writers on one Git
// worktree — the failure the stable hash exists to avoid.
func TestStableHashMatchesSharedVectors(t *testing.T) {
	v := loadVectors(t)
	if len(v.StableHash) == 0 {
		t.Fatal("shared vectors carry no stable_hash cases")
	}
	for _, tc := range v.StableHash {
		if got := StableHashIndex(tc.Key, tc.Count); got != tc.Index {
			t.Errorf("StableHashIndex(%q, %d) = %d, shared vector says %d",
				tc.Key, tc.Count, got, tc.Index)
		}
	}
}

func TestStableHashPickSortsAndRejectsEmptySets(t *testing.T) {
	// The sort is what makes two routers agree: SMEMBERS order is arbitrary, so
	// an unsorted pick would be stable per-process and inconsistent across
	// processes — the worst of both.
	key := "t1/ws-t1-shop/u1/dev"
	forward := StableHashPick(key, []string{"http://a:1", "http://b:1", "http://c:1"})
	reversed := StableHashPick(key, []string{"http://c:1", "http://b:1", "http://a:1"})
	if forward != reversed {
		t.Errorf("pick depends on input order: %q vs %q", forward, reversed)
	}
	if got := StableHashPick(key, []string{"", ""}); got != "" {
		t.Errorf("StableHashPick with no usable candidate = %q, want empty", got)
	}
	// A non-positive count has no valid index; the sentinel must not be usable
	// as one.
	if got := StableHashIndex(key, 0); got != -1 {
		t.Errorf("StableHashIndex(_, 0) = %d, want -1", got)
	}
}

func TestRenewIntervalIsStrictlyMoreFrequentThanTTL(t *testing.T) {
	// A holder renewing once per TTL would lose its lease to one dropped
	// packet; the divisor buys two consecutive failures.
	if RenewDivisor < 3 {
		t.Fatalf("RenewDivisor = %d, want >= 3", RenewDivisor)
	}
	if interval := DefaultTTL / RenewDivisor; interval != 40*time.Second {
		t.Errorf("renew interval = %v, want 40s (must match Python)", interval)
	}
}
