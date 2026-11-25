# Voice-triggered web search

Buddy routes clear spoken searches separately from slide navigation and the live
presentation agent. Text and image searches share the voice command router, but use
different presentation surfaces:

- Image and photo searches open Google Images in Chrome.
- Information and definition questions open a compact Buddy side panel. The current slide
  remains visible behind the panel. A source title opens its HTTPS URL in the default
  browser; closing the panel or saying “back to the presentation” returns to the same
  slide.

Examples include “search for images of cute cats,” “find photos of solar panels,” “what's
the definition of opportunity cost,” “what does amortization mean,” and “look up the
definition of compound interest.” A search must include a concrete query. Incomplete
commands wait briefly for the rest of the phrase. Recent explicit searches can be corrected
with phrases such as “I mean …” before dispatch; a correction replaces the pending query.

Information answers use the authenticated `POST /api/v1/web-search/answer` endpoint. The
backend performs a bounded text search (four results, five-second provider timeout, safe
search enabled), sends the question and result snippets to the configured Cohere Command A
model for a short grounded answer, then returns that answer with the original HTTPS source
links. The model cannot provide or alter source URLs. The desktop sends no Cohere key and
does not save the spoken query or answer. Search providers and Cohere receive the query and
search snippets as needed to provide the requested result.

The feature is available only when the server-side presentation-agent Cohere credential is
configured. Tests and UI code must mock network providers; never use production credentials.
