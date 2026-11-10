# Microphone capture

Buddy can capture microphone audio during an active saved PDF presentation session or an
explicitly connected Windows PowerPoint slideshow. For a PDF, sign in, open the saved deck
from Library, and start the presentation session. For PowerPoint, open a slideshow from
Present or connect to an already running one. Select an input device in Present and choose
Start microphone. Permission is requested only after that command. The on-screen status
reports whether capture is on, off, muted, paused,
or unavailable. Mute and Pause stop capture until their corresponding controls are
cleared. Stop microphone releases the input device.

Changing decks, leaving Present, signing out, losing account access, ending the session,
disconnecting or changing the connected PowerPoint show, or closing Buddy stops capture.
When a PowerPoint status check fails or cannot be refreshed for five seconds, capture
stops until the presenter explicitly starts it again after verification resumes. Slideshow
exit is normally detected by the existing half-second poll. If the selected input
disappears, capture stops and another device can be
chosen explicitly. Reconnecting a device never starts listening automatically. A denied
operating-system permission requires the user to enable access in the system's privacy
settings before retrying.

Capture uses Qt Multimedia in a separate thread. It is not connected to speech recognition
or an agent. Audio is not written to disk or sent to a server. A bounded in-memory queue
holds at most eight 4096-byte chunks and is cleared when capture stops or is muted/paused;
older chunks are dropped when the queue is full. The input's native preferred format is
used. macOS bundles declare microphone usage and an audio-input entitlement.

For a real-device test, sign in, open a saved, ready PDF from Library, and wait for its
presentation session to connect. Select the USB microphone and start capture. Verify the
status changes to Microphone on. Mute, unmute, pause, resume, and stop should update the
status; disconnecting the device should stop capture. End the session or sign out and
confirm that microphone access is no longer active. Saved session support must be deployed
on the configured backend for this flow; local PDF viewing alone does not start a session.

On Windows with PowerPoint installed, open a slideshow or connect to a running slideshow
from Present. Start the microphone and verify mute, pause, resume and stop. Disconnect
Buddy, or end the slideshow in PowerPoint, and confirm capture stops. Reconnecting the
slideshow must not restart the microphone automatically. PowerPoint uses its live local
connection; it does not create a saved PDF backend session.
