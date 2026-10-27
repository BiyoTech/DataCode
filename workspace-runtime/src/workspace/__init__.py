"""Workspace-scoped persistence for workspace-runtime.

Two kinds of tables live under this package (see ``models.py`` for the full
ownership statement):

* tables this service OWNS and creates (apply history, knowledge metadata,
  editor drafts);
* ORM mirror views of control-service's platform metadata (workspaces, git
  credentials), READ ONLY — created and written exclusively by control's
  migrate step and its REST API, never here.
"""
