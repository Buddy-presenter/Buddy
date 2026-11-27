# Jev without Cohere retrieval prototype (#252)

This is an opt-in experiment, not a rollout. The default remains semantic retrieval.
Run the prototype on a separate local API; do not enable it on shared staging or merge it
without reviewing the paired evaluation results.

Set `BUDDY_AGENT_ENABLED=true`, `BUDDY_DECISION_PROVIDER=jev`,
`BUDDY_AGENT_CANDIDATE_MODE=direct_current`, and the server-side `BUDDY_JEV_API_KEY`.
A Cohere key is not required for this decision path. Normal deck analysis providers
remain separate. No provider credential belongs in the desktop or its settings.

The complete menu contains every currently eligible zoom/highlight action on the visible
slide, navigation to every saved slide, no action, and reset when the current view is
focused. Other manual PowerPoint drawing tools are not new live-agent tools in this
experiment. Descriptions and source-measured target geometry use the existing production
builders. The same server and desktop eligibility, authorization, source, freshness,
transition-policy and execution checks still apply.

Jev supports at most 255 choices. A larger complete menu returns the existing safe
`candidates_unavailable` fallback; it is never silently shortened and never falls back to
Cohere. Selecting the Cohere decider in this mode also fails closed. Index preparation
checks session ownership/source and returns ready without document or query embeddings.

For the primary comparison, keep `jev-1.14.0`, its instructions, the 0.6 choice threshold,
and the 0.2 presenting-now threshold identical in both variants. The prototype changes
candidate construction only. Explicit navigation fast paths remain available in both.
The prototype sends contextual navigation through Jev instead of the retrieval-based
topic fast path. Report such routing differences separately.

Evaluation artifacts and measured results will be documented here after the paired
live-provider and native-player checks. A transcript-only replay must never be described
as a microphone test, and successful proposal selection must not count as successful
player execution.
