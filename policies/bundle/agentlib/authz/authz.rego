# The platform's authorization decision.
#
# The policy is a small, fixed evaluator. What is actually allowed lives in a
# rules document, loaded as data at `data.agentlib.rules`. The first rule that
# covers the request allows it and supplies its obligations; a request that no
# rule covers is denied. The in-process `rules` provider of ai-agent-lib-core
# evaluates the same document the same way, and one contract test suite runs
# against both.
#
# Input:  the versioned decision input, schema `agentlib.decision/v1`.
# Output: `data.agentlib.authz.decision`, an object with `allow`,
#         `reason_code` and, on an allow, `obligations`.
#
# The rules document is checked as strictly as the in-process engine checks
# it. A rule with an unknown key, such as `role` for `roles`, would otherwise
# lose its restriction and cover every caller, so a document that is not valid
# denies every request (`invalid_rules`) until it is fixed.
package agentlib.authz

levels := {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}

default decision := {"allow": false, "reason_code": "no_matching_rule"}

decision := {"allow": false, "reason_code": "unsupported_input"} if {
	input.schema != "agentlib.decision/v1"
}

decision := {"allow": false, "reason_code": "invalid_rules"} if {
	input.schema == "agentlib.decision/v1"
	not rules_valid
}

decision := {
	"allow": true,
	"reason_code": rule.id,
	"obligations": object.get(rule, "obligations", {}),
} if {
	input.schema == "agentlib.decision/v1"
	rules_valid
	count(matching) > 0
	rule := matching[0]
}

# Every rule that covers the request, in the order the document lists them.
matching := [rule |
	some rule in data.agentlib.rules.rules
	covers(rule)
]

covers(rule) if {
	input.action in rule.actions
	role_ok(rule)
	agent_ok(rule)
	kind_ok(rule)
	application_ok(rule)
	resource_ok(rule)
	classification_ok(rule)
}

# A rule without roles covers any caller. A rule with roles needs one of them.
role_ok(rule) if object.get(rule, "roles", null) == null

role_ok(rule) if {
	some role in rule.roles
	role in input.principal.roles
}

# The agent is the application that presented the request for the caller: the
# last of the actors in the caller's token. A rule that names agents does not
# cover a request that no agent stands behind.
agent_ok(rule) if object.get(rule, "agents", null) == null

agent_ok(rule) if {
	actors := object.get(input.principal, "actors", [])
	count(actors) > 0
	actors[count(actors) - 1] in rule.agents
}

# A caller is a person ("user") or an application acting for itself ("service").
kind_ok(rule) if object.get(rule, "kinds", null) == null

kind_ok(rule) if object.get(input.principal, "kind", "user") in rule.kinds

application_ok(rule) if object.get(rule, "applications", null) == null

application_ok(rule) if input.context.application in rule.applications

# In a resource pattern `*` matches any text and nothing else is special.
resource_ok(rule) if {
	some pattern in object.get(rule, "resources", ["*"])
	glob.match(pattern, null, input.resource.name)
}

# With a limit, a resource whose classification is unknown is not covered.
classification_ok(rule) if object.get(rule, "max_classification", null) == null

classification_ok(rule) if {
	levels[input.resource.classification] <= levels[rule.max_classification]
}

# ------------------------------------------------- the rules document is valid

rule_keys := {
	"id", "actions", "roles", "agents", "kinds", "applications",
	"resources", "max_classification", "obligations",
}

list_keys := {"roles", "agents", "kinds", "applications"}

actions := {
	"model.route", "tool.call", "data.query", "agent.call",
	"skill.activate", "memory.read", "memory.write",
}

obligation_keys := {"row_filter", "mask_columns", "max_rows", "require_approval"}

# No rules document at all grants nothing, the same as an empty one.
rules_valid if not data.agentlib.rules

rules_valid if {
	document := data.agentlib.rules
	document.schema == "agentlib.rules/v1"
	is_array(object.get(document, "rules", []))
	count(object.keys(document) - {"schema", "rules"}) == 0
	every rule in object.get(document, "rules", []) {
		valid_rule(rule)
	}
	ids := [rule.id | some rule in object.get(document, "rules", [])]
	count(ids) == count({id | some id in ids})
}

valid_rule(rule) if {
	is_object(rule)
	count(object.keys(rule) - rule_keys) == 0
	regex.match(`^[a-z][a-z0-9_-]{0,62}$`, rule.id)
	is_array(rule.actions)
	count(rule.actions) > 0
	every action in rule.actions {
		action in actions
	}
	every key in object.keys(rule) & list_keys {
		names_or_null(rule[key])
	}
	kinds_or_null(object.get(rule, "kinds", null))
	patterns(object.get(rule, "resources", ["*"]))
	classification_or_null(object.get(rule, "max_classification", null))
	valid_obligations(object.get(rule, "obligations", {}))
}

kinds_or_null(value) if value == null

kinds_or_null(value) if {
	is_array(value)
	every kind in value {
		kind in {"user", "service"}
	}
}

names_or_null(value) if value == null

names_or_null(value) if {
	is_array(value)
	every name in value {
		is_string(name)
		count(name) > 0
	}
}

patterns(value) if {
	is_array(value)
	count(value) > 0
	every pattern in value {
		is_string(pattern)
		regex.match(`^[A-Za-z0-9_.*:/-]{1,200}$`, pattern)
	}
}

classification_or_null(value) if value == null

classification_or_null(value) if value in object.keys(levels)

valid_obligations(obligations) if {
	is_object(obligations)
	count(object.keys(obligations) - obligation_keys) == 0
	row_filter := object.get(obligations, "row_filter", {})
	is_object(row_filter)
	every column, values in row_filter {
		is_string(column)
		is_array(values)
	}
	names_or_null(object.get(obligations, "mask_columns", []))
	whole_or_null(object.get(obligations, "max_rows", null))
	is_boolean(object.get(obligations, "require_approval", false))
}

whole_or_null(value) if value == null

whole_or_null(value) if {
	is_number(value)
	value == floor(value)
}
