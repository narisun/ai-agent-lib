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
package agentlib.authz

levels := {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}

default decision := {"allow": false, "reason_code": "no_matching_rule"}

decision := {"allow": false, "reason_code": "unsupported_input"} if {
	input.schema != "agentlib.decision/v1"
}

decision := {
	"allow": true,
	"reason_code": rule.id,
	"obligations": object.get(rule, "obligations", {}),
} if {
	input.schema == "agentlib.decision/v1"
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
