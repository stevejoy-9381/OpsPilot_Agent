# OpsPilot

OpsPilot is an incident-investigation agent for a local lab of toy Docker Compose services: it gathers evidence through bounded, schema-typed read tools, proposes a reversible recovery plan, waits for human approval of that exact plan, executes it through a narrow broker that is the only component allowed to touch lab control, and then independently verifies the outcome. The LLM only investigates and proposes; authorization, approval, execution, and verification are deterministic code.

**Status: foundation only.** This repository currently contains the monorepo skeleton; there is no application logic yet.
