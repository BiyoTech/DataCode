# conf/

All runtime configuration, ONE mechanism: a neutral `platform.yaml` base plus
a pure-increment profile selected by `DATACODE_ENV` and deep-merged on top.
There are no per-deployment full-copy files here — a second copy of the base
is how a stack drifts from the repository (that is exactly what the old
`conf-{docker,cell,global}` directories were, and why they are gone).

## Files

| File | Role |
|---|---|
| `platform.yaml` | The base: pools, timeouts, key prefixes, logging — **zero addresses** |
| `platform.{dev,docker,cell,global,k8s-cell,k8s-global}.yaml` | Pure-increment overlays; `DATACODE_ENV` picks one |
| `workspace-runtime.yaml` / `control-service.yaml` / `agent-service.yaml` / `gateway.yaml` | Per-service settings (ports, subsystem knobs) |
| `isolation-profiles.yaml` | The L1–L3 isolation profiles by task kind (strongest-wins, fail-safe chain) |
| `shell-command-policy.yaml` | Agent shell command policy |

## Rules the loaders and doctor enforce

- **Base carries no addresses.** An address only ever appears in a profile.
- **Secrets are env-only.** No key or token is ever written in YAML; see the
  env registry in `doc/ops/configuration.md`.
- **egress proxy only where a NetworkPolicy enforces it.** `config-doctor`
  rejects `services.egress_proxy` outside the k8s profiles — a proxy without
  enforcement is an illusion of control.
- **Env beats YAML.** A `WORKSPACE_RUNTIME_*`-style env var silently overriding
  (or being silently ignored by) YAML is a class of bug the shared loader
  exists to prevent.

## Inspect what actually took effect

```
make doctor        # effective config per profile + invariant checks
```

Full environment registry and misconfiguration symptoms:
`doc/ops/configuration.md`.
