# Adapters are the only place SDKs live

This layer implements a Protocol from `taskflow.ports` and nothing else. Rules:

- Never catch bare `Exception` to hide a failure. Translate SDK errors into
  `taskflow.domain.errors` types so services stay SDK-agnostic. `TransientError` means
  "retrying may help"; `ConfigError` means "retrying will not".
- Model names, base URLs, timeouts and prices come from `config/*.yaml` via settings —
  never as literals in this code. An adapter must not carry its own price list.
- An adapter reports what it observed, not what it means. Deciding whether a retrieval
  was *sufficient*, or whether a failure is *fatal*, belongs to a service.
- A method that performs an action either returns proof of it or raises. Never return a
  bare `True` on a path that did not do the work.
- LLM adapters must handle the two structured-output failure cases explicitly: a refusal
  stop reason and a max_tokens stop reason both raise `SchemaError`, which triggers
  fallback to the next provider.
