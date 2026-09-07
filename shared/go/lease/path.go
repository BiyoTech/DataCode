package lease

import "path"

// cleanPath collapses "." and ".." and duplicate separators.
//
// Wrapped rather than calling path.Clean directly so the traversal-normalization
// step has one name and one place to change: KeyFromWorkingCopyPath depends on
// the EFFECTIVE segments of a path, and a caller-supplied "../../other-tenant"
// must not be able to address a directory the cleaned form does not contain.
func cleanPath(value string) string {
	if value == "" {
		return ""
	}
	return path.Clean(value)
}
