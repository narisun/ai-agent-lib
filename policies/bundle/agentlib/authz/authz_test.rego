package agentlib.authz_test

import data.agentlib.authz

rules := {
	"schema": "agentlib.rules/v1",
	"rules": [
		{
			"id": "analysts-query-accounts",
			"actions": ["data.query"],
			"roles": ["analyst"],
			"applications": ["accounts-mcp"],
			"resources": ["accounts.*"],
			"max_classification": "restricted",
			"obligations": {
				"row_filter": {"region": ["east", "west"]},
				"mask_columns": ["holder"],
				"max_rows": 50,
			},
		},
		{
			"id": "auditors-query-anything",
			"actions": ["data.query"],
			"roles": ["auditor"],
			"max_classification": "confidential",
		},
		{"id": "anyone-uses-the-default-model", "actions": ["model.route"], "resources": ["default"]},
		{
			"id": "analysts-query-accounts-elsewhere",
			"actions": ["data.query"],
			"roles": ["analyst"],
			"resources": ["accounts.*"],
		},
		{"id": "nobody", "actions": ["tool.call"], "roles": []},
		{
			"id": "tellers-through-the-branch-agent",
			"actions": ["memory.read"],
			"roles": ["teller"],
			"agents": ["branch-agent"],
		},
		{"id": "services-write-memory", "actions": ["memory.write"], "kinds": ["service"]},
	],
}

question := {
	"schema": "agentlib.decision/v1",
	"principal": {"subject": "u-1", "tenant": "t-9", "roles": ["analyst"]},
	"action": "data.query",
	"resource": {"kind": "query", "name": "accounts.by_region", "classification": "restricted"},
	"context": {"application": "accounts-mcp", "environment": "local"},
}

decide(patch) := decision if {
	decision := authz.decision with input as object.union(question, patch)
		with data.agentlib.rules as rules
}

test_a_granted_request_is_allowed_with_the_rules_obligations if {
	decision := decide({})
	decision.allow
	decision.reason_code == "analysts-query-accounts"
	decision.obligations == {
		"row_filter": {"region": ["east", "west"]},
		"mask_columns": ["holder"],
		"max_rows": 50,
	}
}

test_the_first_matching_rule_decides if {
	decision := decide({"context": {"application": "reporting-mcp", "environment": "local"}})
	decision.allow
	decision.reason_code == "analysts-query-accounts-elsewhere"
	decision.obligations == {}
}

test_a_request_no_rule_grants_is_denied_without_obligations if {
	denied := {"allow": false, "reason_code": "no_matching_rule"}
	decide({"principal": {"subject": "u", "tenant": "t", "roles": ["viewer"]}}) == denied
	decide({"principal": {"subject": "u", "tenant": "t", "roles": []}}) == denied
	decide({"action": "tool.call"}) == denied
	decide({"resource": {"kind": "query", "name": "payments.by_region", "classification": "restricted"}}) == denied
	decide({"resource": {"kind": "query", "name": "accountsXby_region", "classification": "restricted"}}) == denied
}

as_auditor(classification) := decide({
	"principal": {"subject": "u", "tenant": "t", "roles": ["auditor"]},
	"resource": object.union({"kind": "query", "name": "ledger.all"}, classification),
})

test_a_rule_covers_data_up_to_its_classification_limit if {
	as_auditor({"classification": "confidential"}).allow
	as_auditor({"classification": "public"}).allow
	not as_auditor({"classification": "restricted"}).allow
	not as_auditor({}).allow
	not as_auditor({"classification": "unheard-of"}).allow
}

route(alias) := decide({
	"principal": {"subject": "u", "tenant": "t", "roles": []},
	"action": "model.route",
	"resource": {"kind": "model", "name": alias},
})

test_a_rule_without_roles_covers_any_caller if {
	route("default").allow
	not route("judge").allow
}

test_a_rule_with_an_empty_role_list_covers_nobody if {
	not decide({"action": "tool.call"}).allow
}

test_an_unknown_input_schema_is_denied if {
	decide({"schema": "agentlib.decision/v2"}) == {"allow": false, "reason_code": "unsupported_input"}
}

test_no_rules_document_denies_everything if {
	decision := authz.decision with input as question
	decision == {"allow": false, "reason_code": "no_matching_rule"}
}

test_an_empty_rules_document_denies_everything if {
	decision := authz.decision with input as question
		with data.agentlib.rules as {"schema": "agentlib.rules/v1", "rules": []}
	decision == {"allow": false, "reason_code": "no_matching_rule"}
}

as_teller(actors) := decide({
	"principal": {"subject": "u", "tenant": "t", "roles": ["teller"], "kind": "user", "actors": actors},
	"action": "memory.read",
})

test_a_rule_that_names_agents_needs_that_agent_to_present_the_request if {
	as_teller(["branch-agent"]).allow
	as_teller(["front-agent", "branch-agent"]).allow
	not as_teller([]).allow
	not as_teller(["other-agent"]).allow

	# Only the agent that presented the request counts, not one further up the chain.
	not as_teller(["branch-agent", "other-agent"]).allow
}

test_a_request_without_an_actors_field_has_no_agent if {
	not decide({
		"principal": {"subject": "u", "tenant": "t", "roles": ["teller"]},
		"action": "memory.read",
	}).allow
}

as_kind(kind) := decide({
	"principal": {"subject": "svc", "tenant": "t", "roles": [], "kind": kind, "actors": []},
	"action": "memory.write",
})

test_a_rule_can_be_limited_to_a_kind_of_caller if {
	as_kind("service").allow
	not as_kind("user").allow
}

# --------------------------------------------- a malformed document grants nothing

with_rule(changes) := decision if {
	rule := object.union(
		{"id": "analysts-query", "actions": ["data.query"], "roles": ["viewer"]},
		changes,
	)
	decision := authz.decision with input as question
		with data.agentlib.rules as {"schema": "agentlib.rules/v1", "rules": [rule]}
}

invalid := {"allow": false, "reason_code": "invalid_rules"}

test_a_misspelt_key_does_not_widen_a_rule if {
	# `role` instead of `roles`: the local engine refuses the file, and so does OPA.
	with_rule({"role": ["viewer"]}) == invalid
}

test_every_part_of_a_rule_is_checked if {
	with_rule({"actions": ["data.querry"]}) == invalid
	with_rule({"actions": []}) == invalid
	with_rule({"roles": "analyst"}) == invalid
	with_rule({"kinds": ["robot"]}) == invalid
	with_rule({"resources": []}) == invalid
	with_rule({"max_classification": "secret"}) == invalid
	with_rule({"obligations": {"mask_column": ["holder"]}}) == invalid
	with_rule({"obligations": {"max_rows": "50"}}) == invalid
	with_rule({"id": "Has Spaces"}) == invalid
}

test_a_valid_rule_still_decides if {
	with_rule({"roles": ["analyst"]}).allow
}

test_two_rules_with_one_id_are_refused if {
	rule := {"id": "same", "actions": ["data.query"], "roles": ["analyst"]}
	decision := authz.decision with input as question
		with data.agentlib.rules as {"schema": "agentlib.rules/v1", "rules": [rule, rule]}
	decision == invalid
}

test_a_document_of_another_schema_is_refused if {
	decision := authz.decision with input as question
		with data.agentlib.rules as {"schema": "agentlib.rules/v2", "rules": []}
	decision == invalid
}
