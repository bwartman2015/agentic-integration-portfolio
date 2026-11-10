# Unit tests for gateway.rego. Run: make opa-test P=01
package ridgeline.gateway_test

import data.ridgeline.gateway

rep := {"client_id": "cs-rep", "scopes": ["crm.read", "erp.read", "shipping.read", "ar.read"], "regions": ["Central"]}

lead := {
	"client_id": "cs-lead",
	"scopes": ["crm.read", "erp.read", "shipping.read", "ar.read", "crm.write"],
	"regions": ["*"],
}

no_scopes := {"client_id": "p01-expiry-test", "scopes": [], "regions": []}

read_tools := ["search_accounts", "get_sales_order", "track_shipment", "get_open_invoices"]

decide(principal, tool, args) := d if {
	d := gateway.decision with input as {"principal": principal, "tool": tool, "args": args}
}

decide_on(principal, tool, resource) := d if {
	d := gateway.decision with input as {"principal": principal, "tool": tool, "args": {}, "resource": resource}
}

test_default_is_deny if {
	gateway.decision == {"allow": false, "reason": "invalid_request"} with input as {}
}

test_rep_may_call_every_read_tool if {
	every tool in read_tools {
		decide(rep, tool, {}) == {"allow": true, "reason": "allowed"}
	}
}

test_rep_may_not_write if {
	decide(rep, "create_case", {}) == {"allow": false, "reason": "missing_scope", "required_scope": "crm.write"}
}

test_lead_may_write if {
	decide(lead, "create_case", {"account_id": "ACCT-1002"}).allow
}

test_client_without_scopes_is_denied_every_tool if {
	every tool, scope in data.gateway_policy.tool_scopes {
		decide(no_scopes, tool, {}) == {"allow": false, "reason": "missing_scope", "required_scope": scope}
	}
}

test_each_tool_needs_exactly_its_own_scope if {
	every tool, scope in data.gateway_policy.tool_scopes {
		others := [s | some s in lead.scopes; s != scope]
		not decide({"client_id": "x", "scopes": others, "regions": ["*"]}, tool, {}).allow
		decide({"client_id": "x", "scopes": [scope], "regions": ["*"]}, tool, {}).allow
	}
}

test_unknown_tool_is_denied_even_for_a_lead if {
	decide(lead, "delete_account", {}) == {"allow": false, "reason": "unknown_tool"}
}

test_pricing_credit_and_terms_changes_are_always_denied if {
	every tool in ["update_pricing", "apply_discount", "set_credit_limit", "change_payment_terms", "REBATE_adjust"] {
		decide(lead, tool, {}) == {"allow": false, "reason": "restricted_change"}
	}
	decide(lead, "create_case", {"credit-limit": 1}) == {"allow": false, "reason": "restricted_change"}
	decide(lead, "create_case", {"payment_terms": "NET60"}) == {"allow": false, "reason": "restricted_change"}
}

test_region_rule_for_reps if {
	decide_on(rep, "get_open_invoices", {"region": "Central"}).allow
	decide_on(rep, "get_open_invoices", {"region": "West"}) == {"allow": false, "reason": "out_of_region"}
	decide_on(rep, "get_open_invoices", {"region": "East"}) == {"allow": false, "reason": "out_of_region"}
}

test_unknown_region_fails_closed_for_reps if {
	every resource in [{}, {"region": null}, {"region": ""}, {"region": 7}] {
		decide_on(rep, "get_sales_order", resource) == {"allow": false, "reason": "region_unknown"}
	}
}

test_lead_sees_every_region_and_unknown_regions if {
	every resource in [{"region": "West"}, {"region": "East"}, {"region": null}, {}] {
		decide_on(lead, "get_sales_order", resource).allow
	}
}

test_missing_regions_claim_sees_nothing if {
	p := {"client_id": "x", "scopes": ["ar.read"]}
	decide_on(p, "get_open_invoices", {"region": "Central"}) == {"allow": false, "reason": "out_of_region"}
}

test_regions_must_be_a_list if {
	every regions in ["Central", "*", {"a": "*"}, {"Central": true}, 7, null] {
		p := {"client_id": "x", "scopes": ["ar.read"], "regions": regions}
		decide_on(p, "get_open_invoices", {"region": "Central"}) == {"allow": false, "reason": "invalid_request"}
	}
	mixed := {"client_id": "x", "scopes": ["ar.read"], "regions": [7, {"a": "*"}, ["*"], "Central"]}
	decide_on(mixed, "get_open_invoices", {"region": "Central"}).allow
	decide_on(mixed, "get_open_invoices", {"region": "West"}) == {"allow": false, "reason": "out_of_region"}
}

test_names_must_be_plain_ascii_identifiers if {
	every tool in ["create_case\n", "créate_case", "", "create case", "pr\u0130ce"] {
		decide(lead, tool, {}) == {"allow": false, "reason": "invalid_request"}
	}
	every key in ["pr\u0130ce", "di\u017fcount", "credit limit", ""] {
		decide(lead, "create_case", {key: 1}) == {"allow": false, "reason": "invalid_request"}
	}
}

test_scope_is_checked_before_the_region if {
	decide_on(no_scopes, "get_open_invoices", {"region": "Central"}).reason == "missing_scope"
}

test_allowed_tools_follow_scopes if {
	gateway.allowed_tools == read_tools_sorted with input as {"principal": rep}
	"create_case" in gateway.allowed_tools with input as {"principal": lead}
	not "create_case" in gateway.allowed_tools with input as {"principal": rep}
	gateway.allowed_tools == [] with input as {"principal": no_scopes}
	gateway.allowed_tools == [] with input as {}
	gateway.allowed_tools == ["search_accounts"] with input as {"principal": {"client_id": "x", "scopes": ["crm.read"]}}
}

read_tools_sorted := sort(read_tools)

test_restricted_tools_are_never_listed if {
	extra := {"tool_scopes": {"update_pricing": "crm.write"}}
	listed := gateway.allowed_tools with input as {"principal": lead}
		with data.gateway_policy as object.union(data.gateway_policy, extra)
	"create_case" in listed
	not "update_pricing" in listed
}

# The same table drives the copilot's offline policy (tests/test_policy.py), so the two cannot drift.
failed_cases contains c.name if {
	some c in data.decision_cases
	got := gateway.decision with input as c.input
	got != c.expect
	print("decision table mismatch:", c.name, "got", got, "want", c.expect)
}

test_shared_decision_table if {
	count(data.decision_cases) >= 30
	count(failed_cases) == 0
}

test_opa_api_only_answers_the_two_decisions_and_health if {
	data.system.authz.allow with input as {"method": "POST", "path": ["v1", "data", "ridgeline", "gateway", "decision"]}
	data.system.authz.allow with input as {
		"method": "POST",
		"path": ["v1", "data", "ridgeline", "gateway", "allowed_tools"],
	}
	data.system.authz.allow with input as {"method": "GET", "path": ["health"]}
	every request in [
		{"method": "PUT", "path": ["v1", "policies", "gateway"]},
		{"method": "PUT", "path": ["v1", "data", "gateway_policy"]},
		{"method": "PATCH", "path": ["v1", "data", "gateway_policy", "tool_scopes"]},
		{"method": "DELETE", "path": ["v1", "policies", "gateway"]},
		{"method": "GET", "path": ["v1", "data"]},
		{"method": "GET", "path": ["v1", "policies"]},
		{"method": "POST", "path": ["v1", "data", "gateway_policy"]},
		{"method": "POST", "path": ["v1", "query"]},
		{"method": "GET", "path": ["v1", "data", "ridgeline", "gateway", "decision"]},
	] {
		not data.system.authz.allow with input as request
	}
}
