# Protobuf Contracts

This directory contains **IDL contract drafts** for future gRPC service-to-service communication.

## Current status

- Services communicate over **HTTP REST** today (via the API Gateway).
- Generated code is **not committed**; run `make proto` to produce `gen/go` and `gen/python`.
- gRPC server implementations are **not yet built** (ports in `conf/*.yaml` are placeholders).

## Services defined

| Proto package | Intended service | Implementation status |
|---------------|------------------|------------------------|
| `agent/v1` | Agent Service | HTTP only |
| `sqlmesh/v1` | SQLMesh Service | HTTP only |
| `workspace/v1` | Workspace Manager | Merged into sqlmesh-service REST |
| `platform/v1` | Platform Service | Not implemented |

## Generate

```bash
make proto   # requires buf CLI
```
