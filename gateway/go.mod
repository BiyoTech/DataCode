module github.com/datacode/gateway

go 1.25.0

// Local shared module, not published. The lease key algebra and Redis layout
// MUST be identical to the Python side; keeping them in one package that both
// the gateway and (via the shared vectors) the Python tests check is what makes
// that verifiable rather than a review convention.
replace github.com/datacode/shared => ../shared/go

require (
	github.com/datacode/shared v0.0.0
	github.com/go-chi/chi/v5 v5.1.0
	github.com/go-chi/cors v1.2.1
	github.com/golang-jwt/jwt/v5 v5.2.1
	github.com/google/uuid v1.6.0
	github.com/redis/go-redis/v9 v9.22.0
	go.uber.org/zap v1.27.0
	gopkg.in/yaml.v3 v3.0.1
)

require (
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/kr/pretty v0.3.0 // indirect
	github.com/rogpeppe/go-internal v1.16.0 // indirect
	github.com/stretchr/testify v1.11.1 // indirect
	go.uber.org/atomic v1.11.0 // indirect
	go.uber.org/multierr v1.11.0 // indirect
	golang.org/x/sys v0.30.0 // indirect
	gopkg.in/check.v1 v1.0.0-20201130134442-10cb98267c6c // indirect
)
