"""Example resource wiring for workspace {{ workspace_name }}.

Resources are NOT auto-discovered as bare objects. To make a resource available
to assets/jobs, return it from a ``@dg.definitions`` function that yields a
``Definitions(resources={...})``; ``load_defs`` merges these into the top-level
Definitions.

Assets consume a resource by declaring a parameter with the resource type, e.g.::

    @dg.asset
    def my_asset(example_api: ExampleApiResource) -> None:
        data = example_api.fetch("/path")

Replace ``ExampleApiResource`` with your real resources (databases, clients...).
"""

import dagster as dg


class ExampleApiResource(dg.ConfigurableResource):
    """Minimal configurable resource scaffold.

    Configuration is validated by Dagster/Pydantic. Keep secrets in env vars and
    pull them via ``dg.EnvVar`` rather than hard-coding.
    """

    base_url: str = "http://localhost"

    def fetch(self, path: str) -> str:
        # Placeholder: implement real client logic here.
        return f"{self.base_url}{path}"


@dg.definitions
def resources() -> dg.Definitions:
    """Wire this workspace's resources into the auto-discovered Definitions."""
    return dg.Definitions(
        resources={
            "example_api": ExampleApiResource(),
        }
    )
