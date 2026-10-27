"""Command dispatch for the agent's shell tool (design 04 §3.6).

Contains the COMMAND ROUTER: the decision of whether a shell command is served
by the governance plane (an environment lock, an audit record, the platform's own
credentials) or by the sandbox. The decision lives here, on the trusted side of
the isolation boundary, so a sandbox never needs the platform's address or the
caller's identity to reach a governed operation.
"""
