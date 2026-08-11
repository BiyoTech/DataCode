"""Dagster Service package.

FastAPI wrapper around a Dagster deployment supporting two modes:

- ``local``:  DagsterInstance (SQLite) + daemon + webserver + Code Location
              gRPC server are all managed as local subprocesses.
- ``remote``: only the Code Location gRPC server runs locally (it must load
              user Python code from ``asset-dag/``); the DagsterInstance
              (daemon + webserver + PostgreSQL) lives on a remote cluster.
"""
