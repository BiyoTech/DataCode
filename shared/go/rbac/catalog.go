// Package rbac holds the platform's permission CATALOG: which permission codes
// exist at all, and how the built-in role templates are composed.
//
// It lives in the shared module because both Go services need the same
// vocabulary and two hand-written copies is exactly how a route ends up guarded
// by a permission no role can hold:
//
//   - identity-service OWNS the data (roles, bindings) and synchronizes this
//     catalog into the permissions table on startup;
//   - the gateway OWNS the decision (route map, scope resolution) and needs the
//     codes at compile time to keep its route table statically checkable.
//
// Responsibility split, unchanged:
//
//   - The CATALOG is declared in code (here, generated from
//     shared/rbac-catalog.json) and never mutated through the API, so a
//     permission code can never be invented by writing to a table. The
//     permissions table is a projection: it renders the role editor and keeps
//     historical role compositions readable after a code is retired.
//   - ROLE COMPOSITION lives in the database and is tenant-editable, including
//     for the built-in roles.
//
// The Python services consume the same declaration through
// datacode_common.rbac; both generated tables are asserted against the JSON by
// their own test suite.
package rbac

// Scope identifies the authorization scope a permission or role belongs to.
// New scopes (e.g. "org", "project") can be added without schema changes.
type Scope string

// PermissionDef is one entry of the permission catalog.
type PermissionDef struct {
	Code        string `json:"code"`
	Scope       Scope  `json:"scope"`
	Resource    string `json:"resource"`
	Action      string `json:"action"`
	Description string `json:"description"`
}

// RoleTemplate describes a built-in role and its default permission set.
//
// The seeder in identity-service writes these as system templates (tenant_id =
// "") and clones them into a tenant the first time it is resolved. A tenant may
// then edit the clone; the seeder never touches tenant-owned rows again.
type RoleTemplate struct {
	Code        string
	Scope       Scope
	Name        string
	Description string
	Permissions []string
}

// PermissionsInScope returns every catalog code of one scope, in declaration
// order. The owner template is built from it so a newly introduced workspace
// permission is part of the role without restating the list.
func PermissionsInScope(scope Scope) []string {
	codes := make([]string, 0, len(Catalog))
	for _, def := range Catalog {
		if def.Scope == scope {
			codes = append(codes, def.Code)
		}
	}
	return codes
}

// IsCatalogPermission reports whether code is a registered permission point.
// Role composition endpoints must reject anything else, and so must any service
// asserting a permission: an unregistered code is unreachable by construction.
func IsCatalogPermission(code string) bool {
	for _, def := range Catalog {
		if def.Code == code {
			return true
		}
	}
	return false
}

// ScopeOf returns the scope a permission code belongs to.
func ScopeOf(code string) (Scope, bool) {
	for _, def := range Catalog {
		if def.Code == code {
			return def.Scope, true
		}
	}
	return "", false
}

// IsValidScope reports whether value is a supported scope name.
func IsValidScope(value string) bool {
	for _, scope := range Scopes {
		if Scope(value) == scope {
			return true
		}
	}
	return false
}

// TemplatesGranting returns the built-in role templates that grant code. An
// empty result means the permission is unreachable through any shipped role,
// which the gateway's route-map test treats as a defect.
func TemplatesGranting(code string) []RoleTemplate {
	var granting []RoleTemplate
	for _, tpl := range BuiltinRoleTemplates {
		for _, granted := range tpl.Permissions {
			if granted == code {
				granting = append(granting, tpl)
				break
			}
		}
	}
	return granting
}
