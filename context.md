MVP name

Collab Loop Studio

MVP user story

A user can:

Create a project room.
Invite another user with a link.
Add a MIDI clip.
Draw notes on a piano roll.
Choose a simple synth sound.
Press play.
Hear a synced loop locally in their browser.
See collaborators’ edits in real time.
Save and reload the project.
Explicitly out of scope for MVP

Do not build these first:

Audio recording
Stem export
VST support
Audio clip warping
Advanced mixing console
Mobile optimisation
Plugin marketplace
Complex permissions
Full sample library
Real-time streamed audio between users

Those turn the project from “hard but buildable” into “massive product.”

2. High-level architecture
Browser client
  ├── React / Next.js UI
  ├── Web Audio / Tone.js playback engine
  ├── Piano roll editor
  ├── Local transport scheduler
  └── Yjs collaborative document

Hostinger VPS
  ├── Nginx reverse proxy
  ├── Next.js app server
  ├── WebSocket sync server
  ├── PostgreSQL database
  ├── Redis, optional for queues/rate limiting
  └── Project persistence service

Use the browser for sound generation because the Web Audio API is built for controlling and processing audio in web apps, including sources, effects, visualisation, and routing. For lower-latency custom DSP later, AudioWorklet runs audio processing code on a separate Web Audio rendering thread.

For collaboration, use Yjs. Its y-websocket provider uses a normal client-server WebSocket model where clients connect to one endpoint, and the server distributes document updates and awareness state.

For musical scheduling, use Tone.js initially. Tone.Transport is designed as a musical timing transport and passes scheduled event times to callbacks, which is better than relying on raw setInterval/requestAnimationFrame for musical playback.

3. Suggested stack
Frontend
Next.js
React
TypeScript
Tailwind
Zustand or Jotai for UI state
Yjs for collaborative document state
y-websocket for sync
Tone.js for initial audio engine
Canvas or SVG for piano roll
Backend
Node.js
Next.js API routes or Fastify/Express service
WebSocket server for Yjs
PostgreSQL
Prisma ORM
PM2 process manager
Nginx reverse proxy
UFW firewall
Certbot HTTPS
VPS services
app.mydomain.com          → Next.js app
ws.mydomain.com           → Yjs WebSocket server
postgres local only       → database
redis local only optional → rate limiting / sessions / queues
4. Core data model
Project
Project {
  id: string
  ownerId: string
  title: string
  bpm: number
  timeSignatureNumerator: number
  timeSignatureDenominator: number
  bars: number
  createdAt: Date
  updatedAt: Date
}
Track
Track {
  id: string
  projectId: string
  name: string
  type: "instrument"
  instrumentType: "synth" | "sampler"
  volume: number
  pan: number
  muted: boolean
  solo: boolean
  color: string
  order: number
}
Clip
Clip {
  id: string
  trackId: string
  startBeat: number
  lengthBeats: number
  loopEnabled: boolean
}
Note
Note {
  id: string
  clipId: string
  pitch: number
  startBeat: number
  durationBeats: number
  velocity: number
}
Collaborator awareness state
Awareness {
  userId: string
  displayName: string
  cursorPosition?: { x: number; y: number }
  selectedNoteIds: string[]
  currentlyEditingClipId?: string
}

For MVP, collaborative state can live primarily in a Yjs document and periodically persist to PostgreSQL.

This will be hosted on Hostinger.
Use docker for local development.
The production domain will be pogojar.com