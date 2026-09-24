---
name: dev-loop
description: Orchestrates a multi-agent loop involving planning, development, review, and publishing sub-agents to implement and publish features test-first and with high standards compliance.
model: inherit
---

# Multi-Agent Iteration Loop

<!-- SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/agents/dev-loop.md -->

Follow the [agent-loop skill](../skills/agent-loop/SKILL.md) (`.agents/skills/agent-loop/SKILL.md`)
step by step — it is the single source for this loop: the phases, the coordinator constraint,
the exit criteria and the skill each sub-agent loads. This agent adds no rules of its own.

The four roles (`planning`, `development`, `review`, `publisher`) are general sub-agents, each
loaded with the skill that agent-loop names for it; Review is the `code-reviewer` agent in
Claude Code. For other harnesses see `.agents/README.md` § Sub-agents across harnesses.
