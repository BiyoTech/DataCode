# Dagster instance configuration for the DATA PLANE (local mode).
#
# In local mode dagster-service runs its own single-process DagsterInstance, and
# Dagster auto-provisions SQLite storage under DAGSTER_HOME when this file is
# absent — the zero-dependency default local development relies on.
#
# Do NOT add PostgreSQL storage here. In remote mode the data plane runs only
# code location gRPC servers and has no instance of its own; the storage that
# matters then belongs to the control plane, configured in
# dagster-control-plane/dagster.yaml.
telemetry:
  enabled: false
