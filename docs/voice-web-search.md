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
the definition of opportunity cost,” “what does amortization mean,” “look up the
definition of compound interest,” “search online for electric cars,” and “Google the Eiffel
Tower.” A search must include a concrete query. Incomplete commands wait briefly for the
rest of the phrase.

Only unmistakable search phrasings start a search. Everyday presenting phrases such as “show
me the revenue chart,” “find the margin,” or “pull up the cost table” belong to Buddy's own
slide commands and the live suggestion path: “show/find/pull up/look for …” searches only
when it asks for images, photos or pictures, or ends with “online,” “on the web” or “on
Google.” Any sentence that Buddy's local slide commands recognise (next/previous slide, zoom,
highlight, reset) is never treated as a search.

Information searches start immediately, so the panel appears as soon as the sentence ends.
Recent explicit searches can still be corrected for three seconds with phrases such as “I
mean …”; a correction sends a new query that replaces the panel's answer. Image searches wait
for that correction window before opening Chrome, which cannot be taken back.

When a PowerPoint slideshow is live, the panel floats over the slideshow's screen, stays on
top of it and does not take keyboard focus, so a clicker or the keyboard keeps controlling
PowerPoint. “Back to the presentation” closes the panel and returns focus to the PowerPoint
slideshow window. PDF presentations use Buddy's own presentation window as before.

Information answers use the authenticated `POST /api/v1/web-search/answer` endpoint. The
backend performs a bounded text search (four results, five-second provider timeout, safe
search enabled), sends the question and result snippets to the configured Cohere Command A
model for a short grounded answer, then returns that answer with the original HTTPS source
links. The model cannot provide or alter source URLs. The desktop sends no Cohere key and
does not save the spoken query or answer. Search providers and Cohere receive the query and
search snippets as needed to provide the requested result.

The feature is available only when the server-side presentation-agent Cohere credential is
configured. Tests and UI code must mock network providers; never use production credentials.
