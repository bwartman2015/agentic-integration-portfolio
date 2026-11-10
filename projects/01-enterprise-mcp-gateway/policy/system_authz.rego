# Who may call OPA's own HTTP API (enabled with --authorization=basic). Default deny: callers may only
# ask for the two gateway decisions and the health check. Nobody can read or replace policies or data
# over the network; they change only by editing the files in this directory and restarting OPA.
package system.authz

default allow := false

allow if {
	input.method == "POST"
	input.path in [
		["v1", "data", "ridgeline", "gateway", "decision"],
		["v1", "data", "ridgeline", "gateway", "allowed_tools"],
	]
}

allow if {
	input.method == "GET"
	input.path == ["health"]
}
