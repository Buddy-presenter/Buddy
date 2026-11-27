# Voice-triggered web search

Buddy routes clear spoken searches separately from slide navigation and the live
presentation agent. Every search shows in a compact Buddy panel over the presentation; the
current slide stays visible behind it:

- Web searches and definition questions (“search for electric cars”, “what does EBITDA
  mean”) show a short grounded answer, up to three small images when relevant, and up to
  four source links.
- Image searches (“show me images of cats”) show a grid of up to nine small images only.
- Clicking a source title or an image opens its HTTPS source page in the default browser.
- The panel takes the top half of the right-hand side of the presentation, with no title,
  logo or echoed query; images are cropped to fill their tiles.
- Scroll with the mouse wheel or by saying “scroll down”/“read more” or “scroll up”/“go
  back up” (these act only while the panel is open).
- Close the panel with its × button, Esc, or by saying “close the panel”, “close the
  results” or “back to the presentation”; the same slide stays on screen.
- Chrome opens only when asked for explicitly (“open Chrome and search for …”), after a
  three-second correction window.

The panel downloads only the small thumbnails the server returns (HTTPS, no redirects,
JPEG/PNG/GIF/WebP, at most 512 KiB each, four-second timeout, in parallel); full-size images
are never fetched, and nothing is saved after the panel closes. Image searches use
DuckDuckGo's image search with safe search on and make no Cohere call.

Examples include “search for images of cute cats,” “find photos of solar panels,” “what's
the definition of opportunity cost,” “what does amortization mean,” “look up the
definition of compound interest,” “search online for electric cars,” and “Google the Eiffel
Tower,” “web search images of dogs” and “do a web search for …”. A search must include a concrete query. Incomplete commands wait briefly for the
rest of the phrase.

Questions open a search only when addressed to Buddy, because presenters often ask them
rhetorically (“So what does EBITDA mean? It's …”): “Buddy, what does EBITDA mean?”, “Buddy,
who invented the telephone?” and “Buddy, how tall is the Eiffel Tower?” search the web and
show a short answer, while the same questions without “Buddy” are ignored. A request to act
(“Buddy, can you show me the revenue chart?”) stays with Buddy's slide commands. Commands
such as “search for”, “web search”, “look up”, “define” and “show me images of” need no
wake word.

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

Answers and images use the authenticated `POST /api/v1/web-search/answer` endpoint with
`mode` `web` (default) or `images`. The
backend performs a bounded text search (four results, five-second provider timeout, safe
search enabled), sends the question and result snippets to the configured Cohere Command A
model for a short grounded answer, then returns that answer with the original HTTPS source
links. The model cannot provide or alter source URLs. The desktop sends no Cohere key and
does not save the spoken query or answer. Search providers and Cohere receive the query and
search snippets as needed to provide the requested result.

The feature is available only when the server-side presentation-agent Cohere credential is
configured. Tests and UI code must mock network providers; never use production credentials.

## Testing search changes without deploying

`BUDDY_WEB_SEARCH_URL` points only the desktop's web-search requests at another server;
everything else keeps using the configured Buddy API. It accepts HTTPS or a loopback HTTP
address (for example `http://127.0.0.1:8765`). A developer can run this branch's
`information_search_router` locally behind a check that staging accepts the same sign-in
token, and start Buddy with the variable set to test the panel end to end.
