package auth

import (
	"context"
	"fmt"
	"strings"
	"time"
)

// Preferences are the per-user display settings.
//
// They live with the USER and globally (design 05 §3): the platform stores UTC
// everywhere and the FRONTEND renders in the user's zone, so the zone must be
// identical in every cell the user's tenants happen to be on.
type Preferences struct {
	// Timezone is an IANA zone name (e.g. "Asia/Shanghai"). Never an offset:
	// offsets do not survive DST transitions.
	Timezone string `json:"timezone"`
	// Locale is the UI language: "zh" or "en".
	Locale string `json:"locale"`
}

// supportedLocales bounds what the UI can actually render.
var supportedLocales = map[string]struct{}{
	"zh": {},
	"en": {},
}

// Validate normalizes and checks the preferences.
//
// The timezone is validated by LOADING it, which is the only way to know the
// runtime can actually resolve the name — a syntactically plausible but
// unknown zone would otherwise fail later, on every render.
func (p *Preferences) Validate() error {
	p.Timezone = strings.TrimSpace(p.Timezone)
	p.Locale = strings.ToLower(strings.TrimSpace(p.Locale))

	if p.Timezone == "" {
		p.Timezone = "UTC"
	}
	if _, err := time.LoadLocation(p.Timezone); err != nil {
		return fmt.Errorf("unknown timezone %q", p.Timezone)
	}
	if p.Locale == "" {
		p.Locale = "en"
	}
	if _, ok := supportedLocales[p.Locale]; !ok {
		return fmt.Errorf("unsupported locale %q (want zh or en)", p.Locale)
	}
	return nil
}

// GetPreferences reads a user's display settings.
func (s *Store) GetPreferences(ctx context.Context, userID string) (Preferences, error) {
	var prefs Preferences
	err := s.db.QueryRowContext(ctx,
		`SELECT timezone, locale FROM users WHERE id = ?`, userID,
	).Scan(&prefs.Timezone, &prefs.Locale)
	if err != nil {
		return Preferences{}, fmt.Errorf("read preferences: %w", err)
	}
	if prefs.Timezone == "" {
		prefs.Timezone = "UTC"
	}
	if prefs.Locale == "" {
		prefs.Locale = "en"
	}
	return prefs, nil
}

// SetPreferences persists a user's display settings.
func (s *Store) SetPreferences(ctx context.Context, userID string, prefs Preferences) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE users SET timezone = ?, locale = ?, updated_at = ? WHERE id = ?`,
		prefs.Timezone, prefs.Locale, formatUTC(time.Now().UTC()), userID)
	if err != nil {
		return fmt.Errorf("update preferences: %w", err)
	}
	return nil
}

// GetPreferences reads a user's display settings through the service.
func (s *Service) GetPreferences(ctx context.Context, userID string) (Preferences, error) {
	return s.store.GetPreferences(ctx, userID)
}

// UpdatePreferences validates and stores a user's display settings.
func (s *Service) UpdatePreferences(
	ctx context.Context, userID string, prefs Preferences,
) (Preferences, error) {
	if err := prefs.Validate(); err != nil {
		return Preferences{}, err
	}
	if err := s.store.SetPreferences(ctx, userID, prefs); err != nil {
		return Preferences{}, err
	}
	return prefs, nil
}
