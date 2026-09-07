// Package lease is the CROSS-LANGUAGE contract for workspace leases
// (design 03 §3.3, design 06 P4a).
//
// Three parties participate in a lease and none of them can be trusted to
// spell it the same way by accident:
//
//   - control-service (Python) grants leases;
//   - workspace-runtime (Python) holds them;
//   - edge-gateway (Go) routes on them.
//
// The Python side of this contract is datacode_common/lease.py and
// datacode_common/naming.py. Every exported symbol here has a counterpart
// there, and the two MUST agree byte for byte: a lease key formatted
// differently in Go than in Python does not fail loudly — it silently produces
// a Redis miss, the gateway asks for a new lease, and a working copy that is
// already mounted elsewhere gets mounted a second time. That is the exact
// failure the lease exists to prevent, so the agreement is verified by tests on
// both sides against the same vectors rather than by review.
//
// This package deliberately contains no I/O and no Redis dependency: it is the
// vocabulary, not the mechanism.
package lease

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
)

// Separator between lease-key segments.
//
// "/" is used because no segment may contain it — tenant, workspace and user are
// filesystem path segments, and environment names match envNamePattern — which
// makes the key unambiguous while staying readable in logs, Redis keys and trace
// attributes. Mirrors _LEASE_SEPARATOR in datacode_common/naming.py.
const Separator = "/"

// arity is the number of segments in a lease key.
const arity = 4

// envsDirName mirrors ENVS_DIR in datacode_common/naming.py: every environment
// working copy is a Git worktree living under this directory.
const envsDirName = "envs"

// workingCopyDepth is how many segments a working copy occupies below the
// materialization root: {tenant}/{workspace}/{user}/envs/{environment}.
const workingCopyDepth = 5

// envNamePattern mirrors ENV_NAME_RE in datacode_common/naming.py.
//
// Kept in sync deliberately rather than relaxed: the gateway rejects a
// malformed key instead of forwarding it, so a pattern that is looser here than
// in Python would let the gateway route a key that the runtime then refuses —
// turning a clear 400 into a confusing 500 from a service the user cannot see.
var envNamePattern = regexp.MustCompile(`^[a-z][a-z0-9_]{1,39}$`)

// reservedEnvNames mirrors RESERVED_ENV_NAMES in datacode_common/naming.py.
// "envs" is reserved because it is the directory holding the worktrees; an
// environment by that name would collide with the layout itself.
var reservedEnvNames = map[string]struct{}{envsDirName: {}}

// ErrMalformedKey is returned for any lease key that is not exactly
// "{tenant}/{workspace}/{user}/{environment}" with a valid environment name.
//
// Coercing instead of failing is what would allow two distinct working copies
// to collapse onto one lease, so this is always an error and never a fallback.
var ErrMalformedKey = errors.New("lease: malformed lease key")

// Key identifies the unit of single-writer ownership: one environment working
// copy of one user.
//
// Why this exact tuple, mirroring the reasoning in datacode_common/naming.py:
// it is the granularity of a Git worktree, and a worktree is what must have a
// single writer. A coarser key (per workspace or per user) would serialize
// unrelated environments onto one instance and destroy the parallel-plan
// property the directory layout was chosen for; a finer one does not exist,
// because nothing below an environment has independent on-disk state.
type Key struct {
	TenantID    string
	WorkspaceID string
	UserID      string
	Environment string
}

// NewKey builds a validated lease key from its parts.
func NewKey(tenantID, workspaceID, userID, environment string) (Key, error) {
	key := Key{
		TenantID:    tenantID,
		WorkspaceID: workspaceID,
		UserID:      userID,
		Environment: environment,
	}
	if err := key.validate(); err != nil {
		return Key{}, err
	}
	return key, nil
}

// ParseKey decomposes a lease key string.
//
// The gateway routes on this without touching the filesystem, so anything
// malformed must be rejected rather than defaulted.
func ParseKey(value string) (Key, error) {
	parts := strings.Split(value, Separator)
	if len(parts) != arity {
		return Key{}, fmt.Errorf(
			"%w: expected {tenant}/{workspace}/{user}/{environment}, got %q",
			ErrMalformedKey, value)
	}
	return NewKey(parts[0], parts[1], parts[2], parts[3])
}

// String renders the canonical wire form. Must be identical to
// datacode_common.naming.lease_key() for the same inputs.
func (k Key) String() string {
	return strings.Join(
		[]string{k.TenantID, k.WorkspaceID, k.UserID, k.Environment}, Separator)
}

func (k Key) validate() error {
	for name, value := range map[string]string{
		"tenant_id":    k.TenantID,
		"workspace_id": k.WorkspaceID,
		"user_id":      k.UserID,
		"environment":  k.Environment,
	} {
		if value == "" {
			return fmt.Errorf("%w: empty %s", ErrMalformedKey, name)
		}
		if strings.Contains(value, Separator) {
			return fmt.Errorf(
				"%w: %s must not contain %q: %q",
				ErrMalformedKey, name, Separator, value)
		}
	}
	if !IsValidEnvName(k.Environment) {
		return fmt.Errorf(
			"%w: invalid environment %q", ErrMalformedKey, k.Environment)
	}
	return nil
}

// IsValidEnvName reports whether an environment name is usable.
// Mirrors datacode_common.naming.is_valid_env_name().
func IsValidEnvName(name string) bool {
	if _, reserved := reservedEnvNames[name]; reserved {
		return false
	}
	return envNamePattern.MatchString(name)
}

// KeyFromWorkingCopyPath derives the lease key from a workspace_path.
//
// The path is the only routing input most requests carry, and its layout is
// "{base}/{tenant}/{workspace}/{user}/envs/{environment}". Segments are read
// from the END because the materialization root is a deployment setting the
// gateway does not know. Asserting the "envs" level is what rejects the shared
// object database (".repo"), the user root itself, and any arbitrary nested
// folder — none of which is a leasable unit.
//
// Mirrors datacode_common.naming.parse_working_copy(), minus the filesystem
// resolution that only the owning service can do.
func KeyFromWorkingCopyPath(workspacePath string) (Key, error) {
	if workspacePath == "" {
		return Key{}, fmt.Errorf("%w: empty workspace path", ErrMalformedKey)
	}
	// Normalize separators and collapse "..", so a traversal cannot smuggle in
	// another tenant's directory: the effective segments are what matter.
	cleaned := cleanPath(strings.ReplaceAll(workspacePath, `\`, "/"))
	segments := strings.Split(strings.Trim(cleaned, "/"), "/")
	if len(segments) < workingCopyDepth {
		return Key{}, fmt.Errorf(
			"%w: %q is not an environment working copy", ErrMalformedKey, workspacePath)
	}

	tail := segments[len(segments)-workingCopyDepth:]
	if tail[3] != envsDirName {
		return Key{}, fmt.Errorf(
			"%w: working copies must live under %q: %q",
			ErrMalformedKey, envsDirName, workspacePath)
	}
	return NewKey(tail[0], tail[1], tail[2], tail[4])
}
