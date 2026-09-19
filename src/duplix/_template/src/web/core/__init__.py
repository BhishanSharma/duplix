"""HTTP plumbing shared by every API module.

Deliberately knows nothing about flights, rosters or the solver — it is
the transport layer the ``api`` package plugs handlers into.
"""
