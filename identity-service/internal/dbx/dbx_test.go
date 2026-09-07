package dbx

import "testing"

// Rebind is the one place where a subtle bug would silently corrupt every
// PostgreSQL query, so its edge cases are pinned here.
func TestRebind(t *testing.T) {
	cases := []struct {
		name    string
		dialect Dialect
		query   string
		want    string
	}{
		{
			name:    "postgres numbers placeholders in order",
			dialect: Postgres,
			query:   "SELECT id FROM users WHERE username = ? AND status = ?",
			want:    "SELECT id FROM users WHERE username = $1 AND status = $2",
		},
		{
			name:    "mysql is untouched",
			dialect: MySQL,
			query:   "SELECT id FROM users WHERE username = ?",
			want:    "SELECT id FROM users WHERE username = ?",
		},
		{
			name:    "sqlite is untouched",
			dialect: SQLite,
			query:   "SELECT id FROM users WHERE username = ?",
			want:    "SELECT id FROM users WHERE username = ?",
		},
		{
			// A literal is not a bind marker: counting it would shift every
			// following parameter by one.
			name:    "postgres skips question marks inside literals",
			dialect: Postgres,
			query:   "INSERT INTO t (a, b) VALUES (?, 'why?') /* c */",
			want:    "INSERT INTO t (a, b) VALUES ($1, 'why?') /* c */",
		},
		{
			name:    "postgres handles empty and quoted literals between binds",
			dialect: Postgres,
			query:   "INSERT INTO m VALUES (?, ?, 'user', ?, '', ?)",
			want:    "INSERT INTO m VALUES ($1, $2, 'user', $3, '', $4)",
		},
		{
			name:    "postgres keeps queries without placeholders intact",
			dialect: Postgres,
			query:   "SELECT COUNT(*) FROM users",
			want:    "SELECT COUNT(*) FROM users",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Rebind(tc.dialect, tc.query); got != tc.want {
				t.Errorf("Rebind()\n got: %s\nwant: %s", got, tc.want)
			}
		})
	}
}

func TestNormalize(t *testing.T) {
	cases := map[string]struct {
		want Dialect
		ok   bool
	}{
		"sqlite":     {SQLite, true},
		"mysql":      {MySQL, true},
		"postgres":   {Postgres, true},
		"postgresql": {Postgres, true}, // spelling used by platform.yaml
		"  MySQL  ":  {MySQL, true},    // config files are hand-edited
		"oracle":     {"", false},
		"":           {"", false},
	}

	for input, want := range cases {
		got, ok := Normalize(input)
		if got != want.want || ok != want.ok {
			t.Errorf("Normalize(%q) = (%q, %v), want (%q, %v)",
				input, got, ok, want.want, want.ok)
		}
	}
}

func TestPlaceholders(t *testing.T) {
	cases := map[int]string{0: "", -1: "", 1: "?", 3: "?,?,?"}
	for n, want := range cases {
		if got := Placeholders(n); got != want {
			t.Errorf("Placeholders(%d) = %q, want %q", n, got, want)
		}
	}
}

func TestSplitIndexDef(t *testing.T) {
	name, target, err := splitIndexDef("idx_users_name ON users (username)")
	if err != nil {
		t.Fatalf("splitIndexDef: %v", err)
	}
	if name != "idx_users_name" || target != "users (username)" {
		t.Errorf("got (%q, %q)", name, target)
	}

	if _, _, err := splitIndexDef("idx_without_target"); err == nil {
		t.Error("expected an error for a definition without ON")
	}
}
