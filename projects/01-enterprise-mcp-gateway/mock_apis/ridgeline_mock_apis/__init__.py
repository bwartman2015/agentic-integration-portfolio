"""Mock carrier and AR HTTP APIs over the seeded synthetic Ridgeline dataset (`mock_systems/`).

They stand in for a carrier's tracking API and the AR system's API so the gateway's connectors make
real, authenticated HTTP calls. Each refuses to start without its shared secret (fail closed).
"""
