# Speech-triggered saved actions (#202)

When a presenter talks about something on the slide, Buddy can run the matching saved
zoom or highlight action that was generated during processing. It only ever runs an
action that has been **executed and visually verified** for this exact deck revision
([action checks and live gating](preflight-publication.md)); an action merely existing
never triggers anything.

## Path from speech to action

1. **Speech:** recognized speech (Deepgram) enters the rolling context
   ([transcript context](transcript-context.md)). Speech that started before a
   setting change, slide move or reconnect is discarded.
2. **Candidates:** the server offers the decision model only the current slide, nearby
   or named slides, "no change", and up to **three** saved region actions that are
   live-usable (validated **and** visually verified). Region actions are offered only for
   the slide on screen, since the desktop never zooms another slide; speech about another
   slide's content selects that slide first (#187). Production retrieval uses Cohere
   embeddings over each action's purpose **and the slide text it targets**; without
   embeddings the word-based resolver does the same ([candidates](presentation-candidates.md)).
   Unverified actions are never offered.
3. **Decision:** the constrained selector (#20, [presentation agent](presentation-agent.md))
   chooses one offered option or abstains. It abstains on ambiguous, negated,
   hypothetical, quoted or unrelated speech; free-form regions are impossible.
4. **Policy:** the transition policy (#21, [transition policy](transition-policy.md))
   applies freshness, duplicate, current-focus, cooldown and evidence rules. Slide moves,
   zoom, highlight and reset run without a second server round trip: the `/agent`
   request that proposed them re-read the session and analysis moments earlier, and the
   desktop verifies the exact saved action itself (#237). Explicit commands such as "zoom
   into the table" or "highlight the total" are resolved on the device, like "next slide",
   when their words single out one saved, verified action of that type on the slide on
   screen; anything less clear goes to the server's contextual decision. Words match the
   action's purpose and the slide text it shows (the PDF grounding quote or PowerPoint
   target/shape text), with spoken units folded ("48 million" matches "$48M") (#239).
   Words that only one action contains, especially numbers, outweigh words several actions
   share, so a shared word like "revenue" no longer makes commands ambiguous (#241). A
   plain statement that says a figure (a number of two or more digits, or any number with
   million/billion/thousand) which the shown text of exactly one verified zoom or highlight
   on the slide contains runs that action locally too ("revenue came in at 48 million");
   questions, hedges and negations, or a figure several actions show, go to the server
   (ambiguous figures log `local_mention_unresolved`).
   Short whole-sentence moves ("Okay, next.", "Let's move on.", "Go back a slide.") also run
   locally; a sentence that names a topic ("moving on to costs") goes to the server (#243).
   "Remove the highlight", "zoom out" and "show the whole slide" reset the view the same
   way. A recognised command that cannot be resolved locally logs
   `presentation_policy event=local_command_unresolved tool=… reason=…` (for example
   `no_matching_words`, `ambiguous`, `no_verified_action_of_type`) without speech text.
   Any proposal the desktop cannot run (automatic actions off, presentation still loading,
   speech changed, action on another slide, saved action unavailable) is logged as
   `presentation_policy event=proposal_dropped decision_id=… reason=…` and explained in
   Present's status line; a valid suggestion is never discarded silently (#187).
5. **Desktop verification and execution:**
   - **PDF:** Present loads the session's saved actions automatically and runs a speech
     action only if it matches a loaded, eligible saved action by ID, type and region.
     It executes that saved action's verified geometry in the PDF player.
   - **PowerPoint:** the saved action is looked up by ID and must be visually verified
     with an identical region before the native Office command runs.
6. **Session:** the accepted action ID and focus are saved with version checks, so
   reconnects restore the same view.

Manual navigation, full-slide view, **Undo automatic move** (Ctrl+Shift+Z), pause and
the presenter's suggestion/automatic toggles always remain available.

## Status the presenter sees

- After a run: `Highlighted: <action purpose>. Undo is available.` (or `Zoomed: …`).
- A reference to an action that is not verified: `Buddy matched an action that is not
  verified for live use, so it did nothing.`
- Saved actions still loading: Buddy says so and acts on the next mention.
- Abstention and other rejections keep the existing policy wording.

## Evidence and privacy

`presentation_latency event=action_finished` logs the decision ID, format, tool, the
saved **action ID**, outcome and timings (capture to action, proposal to action,
server check, executor). `presentation_policy event=saved_action_rejected` records
the reason and action ID. Transcript text, slide text, action purposes and secrets are
never logged.

## Testing

- `backend/tests/api/test_speech_saved_actions.py`: real session, candidate, agent and
  transition-check API with PostgreSQL; the decision model is a deterministic stand-in.
  Covers selection of the exact saved action, abstention, and refusal of unverified,
  unknown or region-mismatched actions.
- `frontend/tests/test_transition_player.py`: the real PDF player executes only the
  exact saved action; unloaded, unverified, unusable, wrong-type or wrong-region
  proposals never execute, with sanitized evidence.
- End-to-end acceptance runs real uploads, analysis, checks, sessions, the PDF player and
  desktop PowerPoint, injecting recognized speech at the speech-recognition boundary.
  Real-voice acceptance with the production decision model runs on staging (see the PR).
