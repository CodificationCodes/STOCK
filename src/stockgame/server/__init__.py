"""Server package: the authoritative game world.

Importing this package does not pull in FastAPI; the HTTP/WebSocket layer
lives in :mod:`stockgame.server.app` so that tests and the admin CLI can use
the services without a web framework in the way.
"""
