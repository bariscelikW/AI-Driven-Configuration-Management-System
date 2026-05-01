# AI-Assisted Configuration Tool (Local LLM + Microservices)

Design Decisions and Implementation Notes


## Overview

This project implements a local, AI-driven configuration management system composed of three microservices: a Schema Service, a Values Service, and a Bot Service. Users can modify application configuration through natural language, and the system applies those changes safely against a JSON Schema.

This document explains the reasoning behind the key decisions I made during development.

---

## Architecture and Service Communication

The system follows a straightforward microservice pattern. All three services are built with FastAPI and run in separate Docker containers on an internal Docker network.

The Schema Service (port 5001) and Values Service (port 5002) are simple file-serving endpoints. They read JSON files from the shared `/data` directory (mounted as a read-only volume) and return them via `GET /{app_name}`. I kept them minimal on purpose because there's no reason to overcomplicate a file-serving endpoint.

The Bot Service (port 5003) is the core of the system. It communicates with the other services over HTTP using the Docker internal DNS names (`schema-server`, `values-server`). It also communicates with an Ollama container that runs the LLM with GPU passthrough.

A separate `ollama-init` container handles model downloading on first startup. It waits for Ollama to be healthy, pulls the model, and exits. The Bot Service only starts after this init step completes. This means the reviewer can run `docker compose up` and everything works automatically without manual model setup.

All services are configured with `restart: always` so they recover automatically if any container crashes.

---

## End-to-End Flow of a User Request

Here is what happens step by step when a user sends a request like `"set tournament service memory to 1024mb"`:

**Step 1 — User sends a POST request to the Bot Service:**
```
POST http://localhost:5003/message
{"input": "set tournament service memory to 1024mb"}
```

**Step 2 — Phase 1: Application Identification.**
The Bot Service sends the user's message to the LLM and asks it to classify which application is being referenced. The LLM returns a single word: `tournament`. No schema or values are involved at this stage.

**Step 3 — Fetching Application Data.**
The Bot Service makes two HTTP calls:
- `GET http://schema-server:5001/tournament` → retrieves the JSON Schema
- `GET http://values-server:5002/tournament` → retrieves the current values JSON

**Step 4 — Phase 2: Path Identification.**
The Bot Service flattens the current values into dot-notation paths (e.g., `workloads.statefulsets.tournament.containers.tournament.resources.memory.limitMiB`), scores them against keywords from the user input, and sends the top candidates to the LLM along with the original user message. The LLM picks the correct path and extracts the intended value. It returns a small JSON object like:
```json
{"path": "workloads.statefulsets.tournament.containers.tournament.resources.memory.limitMiB", "action": "set_to", "value": "1024"}
```

**Step 5 — Phase 3: Apply and Validate.**
The Bot Service applies the change to the original values using Python dictionary operations. It handles unit conversion (MB, GB), percentage calculations, and auto-syncing of related fields (e.g., keeping request and limit equal when they were equal before). The modified values are then validated against the full JSON Schema. If validation passes, the complete modified values JSON is returned to the caller.

**Step 6 — Response.**
The user receives the full updated values JSON with only the requested field changed and all other fields preserved exactly as they were.

---

## Why I Changed the Model from LLaMA 3.2 to Qwen 2.5 Coder

I started with `llama3.2:3b` because it was already on my machine. The first version of the bot tried to have the LLM regenerate the entire values JSON after modification. This approach was painfully slow — a single request to the tournament service took around 5 minutes. The tournament schema is roughly 1,770 lines, and forcing a 3B model to output 190 lines of JSON while keeping every field intact was simply not realistic. I tried giving the schemas and values to Grok to see what should be expected time for this case and it made the requested change in about 2–3 seconds. So I knew the approach needed to improve significantly, no one use this 5 minutes system. 

After reworking the prompting strategy, the response time dropped to about 2 minutes, but the accuracy was still inconsistent. LLaMA 3.2 would sometimes pick the wrong JSON path, ignore instructions about which fields to change, or fail to do basic arithmetic like calculating 80% of 1500.

I switched to `qwen2.5-coder:3b` for a few reasons. First, it's specifically trained on code and structured data, so it handles JSON much more reliably. Second, it fits comfortably in my 4GB VRAM (RTX 3050 Laptop), meaning the entire model stays on the GPU without CPU offloading. Third, with the final architecture, response times dropped to 3–4 seconds per request. That's a massive improvement from where I started.

---

## The Three-Phase Approach

Early on I realized that asking a small LLM to do everything — understand the user's intent, navigate a huge schema, and produce correct JSON — was setting it up to fail. So I split the problem into parts where each component does what it's best at.

**Phase 1** is trivial: the LLM picks one of three application names. This is basically a classification task and it works reliably every time.

**Phase 2** is the interesting part. Instead of dumping the entire schema and values into the prompt and hoping for the best, I flatten the current values JSON into dot-notation paths. Then I score these paths against keywords from the user's input and send only the top candidates to the LLM. The model's job is just to pick the correct path and extract the new value from the user's message. This is a much simpler task for a 3B model than regenerating an entire configuration file.

**Phase 3** is pure Python. The LLM doesn't do any math or JSON manipulation. All numeric calculations — percentage computation, unit conversion (MB, GB), value parsing — happen deterministically in code. The modification is applied using standard dictionary operations, and then validated against the full JSON Schema using the `jsonschema` library. This way, even if the LLM occasionally returns something unexpected, the validation layer catches it before the response goes out.

---

## Resource Requests vs. Limits

Since this system modifies Kubernetes-style configurations, I want to briefly explain how `resources.cpu` and `resources.memory` work, because this informed some of the logic in the code.

In Kubernetes, `request` is the minimum amount of resources guaranteed to a container. The scheduler uses this value to decide which node to place the pod on. `limit` is the maximum the container is allowed to consume. If it tries to exceed the memory limit, it gets killed. If it exceeds the CPU limit, it gets throttled.

The rule is that `request` must always be less than or equal to `limit`. In many production environments, teams set `request = limit` intentionally. This gives the pod a "Guaranteed" QoS class, which means Kubernetes will not evict it under memory pressure. It's a common pattern for critical services.

I implemented auto-sync logic in the bot to handle this. If the original configuration has `request == limit` (Guaranteed QoS), and the user changes the limit, the code automatically updates the request to match. It also handles the case where a new limit would be lower than the existing request, which would be an invalid state — in that case, the request is lowered to match the new limit. This prevents the system from producing configurations that would be rejected by Kubernetes.

---

## Assumptions and Deviations

The README describes a flow where the full JSON Schema and full values JSON are sent to the LLM in the second call, and the LLM returns the complete modified values JSON directly. I deviated from this in two ways:

1. Instead of sending the full schema and values, I send a filtered list of candidate paths extracted from the values. The schema is still fetched and used — but for validation after the change is applied, not as input to the LLM prompt. The reason is practical: the tournament schema alone is 1,770 lines. Sending that much context to a 3B model causes it to lose track of the actual task, produce incomplete output, or drop fields entirely. Filtering the candidates keeps the prompt small and focused.

2. Instead of having the LLM return the full modified values JSON, the LLM returns a small patch (which path to change, and to what value). Python then applies this patch to the original values and returns the complete modified JSON. This guarantees that no fields are accidentally dropped or altered, which was a recurring problem when the LLM tried to regenerate the entire file.

Both deviations were made because the original approach was unreliable with the hardware and model size available. The end result is the same — the caller receives a complete, schema-validated, modified values JSON — but the internal mechanism is more robust. The README states that "reasonable assumptions are allowed as long as they are documented," so I'm documenting them here.

---

## Testing

I tested the system with 7 different requests covering the main use cases:

The first three are the examples from the README:
- Setting tournament service memory to 1024MB
- Setting the GAME_NAME environment variable to "toyblast" for the matchmaking service
- Lowering the CPU limit of the chat service to 80% of its current value

I then ran four additional tests to check broader coverage:
- Changing the replica count of the tournament service
- Updating the container image version for the chat service
- Modifying the HTTP port of the matchmaking service
- Setting an absolute CPU request value for matchmaking

All seven tests passed. The schema validation confirmed that every output was structurally valid, and I manually verified that the correct fields were changed with the expected values.

---

## Performance Summary

| Stage | Model | Approach | Time per Request |
|---|---|---|---|
| Initial attempt | llama3.2:3b | Full JSON regeneration | ~5 minutes |
| Improved prompting | llama3.2:3b | Full JSON regeneration | ~2 minutes |
| Final implementation | qwen2.5-coder:3b | Path selection + Python apply | 3–4 seconds |

The final architecture is fast because the LLM only generates a small JSON object (path + value), and all the heavy lifting — modification, validation, edge case handling — is done in Python.

---

## Final Notes

The main takeaway from this project is that small LLMs work best when you give them small, well-defined tasks. Asking a 3B model to regenerate a 190-line JSON file while following a 1,770-line schema is not a reasonable expectation. But asking it to pick a path from a list of 20 candidates and extract a value from a sentence — that it can do reliably and quickly.

I tried to keep the codebase simple. Each service is under 100 lines except the bot, and even that is mostly helper functions for JSON traversal. There's no caching, no async LLM calls, no retry logic beyond what FastAPI gives you — because for this use case, none of that is needed. The system does one thing and does it correctly.

## Production Considerations

This project is a Proof of Concept (PoC) and is not intended for production use. The current design focuses on simplicity and local LLM usage.

### Reliability
- LLM output may be invalid → must be parsed and validated
- Timeouts and retries should be added for LLM and service calls
- Schema/Values services may be unavailable → handle failures gracefully
- All outputs must pass JSON Schema validation before returning

### Security
- User input is treated as untrusted
- LLM output is never executed directly
- Changes are applied deterministically in Python
- No arbitrary code execution is allowed

### Limitations
- Works only with predefined schemas and applications
- No authentication, logging, or version control integration


----

NOTE: This project was developed as a technical case study!

Barış Çelik

 bariscelikww@gmail.com