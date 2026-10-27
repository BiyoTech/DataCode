# Dagster workspace configuration (standalone reference).
# The dagster-service normally generates a dynamic workspace.yaml listing the
# gRPC code locations it manages. This file is a standalone fallback that loads
# the local definitions.py directly for `dagster dev` usage.
load_from:
  - python_file:
      relative_path: definitions.py
      location_name: {{ location_name }}
