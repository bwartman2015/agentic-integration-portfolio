# Ridgeline MCP gateway authorization (Milestones 4-5). Default deny.
#
# Input (built by the gateway from the verified token and the call; never from caller-supplied identity):
#   {"principal": {"client_id": "cs-rep", "scopes": ["crm.read", ...], "regions": ["Central"]},
#    "tool": "get_open_invoices", "args": {"account_id": "ACCT-1002"},
#    "resource": {"region": "Central"}}          # only on the data check, once the record is resolved
#
# decision      {"allow": bool, "reason": code[, "required_scope": scope]}
#               reasons, first match wins: invalid_request, restricted_change, unknown_tool,
#               missing_scope, region_unknown, out_of_region, allowed. The list is
#               data.gateway_policy.reasons: the gateway and the copilot read the same list.
#               invalid_request: any part of the input has the wrong type, or the tool name or an
#               argument name (at any depth) is not a plain ASCII identifier (so the rules below, and the copilot's
#               offline copy of them, never depend on how a regex engine folds Unicode)
# allowed_tools the tools this principal's scopes cover (used to filter tools/list)
#
# Rules
#   1. Pricing, credit-limit and payment-term changes are never allowed, whatever the tool is called
#      and however deeply the argument is nested.
#   2. A tool runs only if the caller holds the scope data.gateway_policy.tool_scopes maps it to.
#      A tool that is not in the map is denied.
#   3. Data rule: when a resource is given, its region must be one of the caller's regions. A resource
#      whose region is unknown is denied. Only the "*" region (CS leads) sees every region.
package ridgeline.gateway

policy := data.gateway_policy

default decision := {"allow": false, "reason": "invalid_request"}

decision := verdict if valid_input

verdict := {"allow": false, "reason": "restricted_change"} if {
	restricted
} else := {"allow": false, "reason": "unknown_tool"} if {
	not known_tool
} else := {"allow": false, "reason": "missing_scope", "required_scope": required_scope} if {
	not has_scope
} else := {"allow": false, "reason": "region_unknown"} if {
	has_resource
	not all_regions
	not known_region
} else := {"allow": false, "reason": "out_of_region"} if {
	has_resource
	not all_regions
	not region_visible
} else := {"allow": true, "reason": "allowed"}

valid_input if {
	is_string(input.tool)
	regex.match(policy.name_pattern, input.tool)
	is_string(input.principal.client_id)
	is_array(input.principal.scopes)
	is_array(object.get(input.principal, "regions", []))
	is_object(object.get(input, "args", {}))
	is_object(object.get(input, "resource", {}))
	every key in arg_keys {
		regex.match(policy.name_pattern, key)
	}
}

# Every argument name at any depth: {"fields": {"credit_limit": 1}} and [{"discount": 5}] count too.
arg_keys contains key if {
	walk(object.get(input, "args", {}), [path, _])
	some key in path
	is_string(key)
}

scopes contains s if {
	some s in input.principal.scopes
	is_string(s)
}

regions contains r if {
	some r in input.principal.regions
	is_string(r)
}

known_tool if input.tool in object.keys(policy.tool_scopes)

required_scope := policy.tool_scopes[input.tool]

has_scope if required_scope in scopes

# Rule 1: by tool name, and by argument name (at any depth) on any write tool.
restricted if regex.match(policy.restricted_pattern, input.tool)

restricted if {
	input.tool in policy.write_tools
	some key in arg_keys
	regex.match(policy.restricted_pattern, key)
}

# Rule 3
has_resource if is_object(input.resource)

all_regions if "*" in regions

known_region if {
	is_string(input.resource.region)
	input.resource.region != ""
}

region_visible if input.resource.region in regions

allowed_tools := sort([tool |
	some tool, scope in policy.tool_scopes
	scope in scopes
	not regex.match(policy.restricted_pattern, tool)
])
