package auth

import (
	"sync"
	"time"
)

// loginAttempt tracks consecutive failures for one login target.
type loginAttempt struct {
	failures    int
	lockedUntil time.Time
	lastFailure time.Time
}

// RateLimiter throttles brute-force login attempts per (username, client IP)
// key. It is intentionally in-memory: the gateway is the single auth entry
// point and the goal is slowing down guessing, not distributed accounting.
type RateLimiter struct {
	mu          sync.Mutex
	attempts    map[string]*loginAttempt
	maxFailures int
	lockout     time.Duration
	lastSweep   time.Time
}

// NewRateLimiter builds a limiter locking a key for lockout after maxFailures
// consecutive failures.
func NewRateLimiter(maxFailures int, lockout time.Duration) *RateLimiter {
	if maxFailures <= 0 {
		maxFailures = 5
	}
	if lockout <= 0 {
		lockout = 5 * time.Minute
	}
	return &RateLimiter{
		attempts:    make(map[string]*loginAttempt),
		maxFailures: maxFailures,
		lockout:     lockout,
		lastSweep:   time.Now(),
	}
}

// Allowed reports whether a key may attempt a login, and the remaining
// lockout duration when it may not.
func (rl *RateLimiter) Allowed(key string) (bool, time.Duration) {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	rl.sweepLocked()

	entry, ok := rl.attempts[key]
	if !ok {
		return true, 0
	}
	if now := time.Now(); entry.lockedUntil.After(now) {
		return false, time.Until(entry.lockedUntil)
	}
	return true, 0
}

// RecordFailure increments the failure counter and locks the key once the
// configured threshold is reached.
func (rl *RateLimiter) RecordFailure(key string) {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	rl.sweepLocked()

	now := time.Now()
	entry, ok := rl.attempts[key]
	if !ok {
		entry = &loginAttempt{}
		rl.attempts[key] = entry
	}
	// A previous lockout that already elapsed starts a fresh window.
	if !entry.lockedUntil.IsZero() && entry.lockedUntil.Before(now) {
		entry.failures = 0
		entry.lockedUntil = time.Time{}
	}
	entry.failures++
	entry.lastFailure = now
	if entry.failures >= rl.maxFailures {
		entry.lockedUntil = now.Add(rl.lockout)
	}
}

// Reset clears the counters for a key after a successful login.
func (rl *RateLimiter) Reset(key string) {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	delete(rl.attempts, key)
}

// sweepLocked drops stale entries so the map cannot grow unbounded. Callers
// must already hold the mutex.
func (rl *RateLimiter) sweepLocked() {
	now := time.Now()
	if now.Sub(rl.lastSweep) < time.Minute {
		return
	}
	rl.lastSweep = now
	// Entries idle for more than two lockout windows carry no useful state.
	staleBefore := now.Add(-2 * rl.lockout)
	for key, entry := range rl.attempts {
		if entry.lockedUntil.After(now) {
			continue
		}
		if entry.lastFailure.Before(staleBefore) {
			delete(rl.attempts, key)
		}
	}
}
