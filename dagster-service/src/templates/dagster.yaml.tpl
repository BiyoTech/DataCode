# Dagster instance configuration (local mode).
# When omitted, Dagster auto-provisions SQLite storage under DAGSTER_HOME,
# which is the zero-dependency default we rely on. This file only disables
# telemetry explicitly; add PostgreSQL storage blocks here for remote setups.
telemetry:
  enabled: false
