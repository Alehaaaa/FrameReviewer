# FrameReviewer

A media review and crop tool for Autodesk Maya, with frame-accurate FFmpeg playback, in/out marks, review annotations, animated crop tracking, and export.

FrameReviewer is a Python package designed to live in Maya's scripts directory.

## Features

- Media playback for video clips and image sequences, including scrubbing, variable speed, marked-range looping, volume/mute, and a rolling FFmpeg frame cache.
- OpenRV-style sequence handling: `@`/`#`/`%0Nd`/Maya-tick frame patterns and Shake-style frame ranges are understood, and discovery preserves holes and negative or non-zero source frame numbers rather than collapsing them onto a zero-based index.
- **Playback → Missing Sequence Frames** offers Skip Missing (legacy dense order), Hold, Black, Checker and Error. The selected interpretation follows the logical source through MediaInfo, Player, the shared Sequence/Grid/Difference time maps and `.frsession` save/reopen. Negative and non-zero source frames, rational FPS, note IDs and recorded trims remain explicit. Intentional holes are reported separately from corrupt-file decode failures; Hold retains the original EXR pixel identity. Large spans are bounded rather than allocating unlimited placeholders.
- Authored pattern ranges such as `plate.999-1009x2#.exr` also preserve missing **leading/trailing** frame positions across session reopen. Matching neighboring files outside a selected time range do not silently enter the sequence. A corrupt existing image displays a decoder-failure slate and diagnostic instead of retaining an unrelated old frame; intentional missing frames use their selected slate/hold policy and remain distinct in the frame tooltip.
- Realtime playback (maintains the audio clock and may skip late video frames)
  or deterministic **Play All Frames** playback for animation inspection.
- Optional thumbnail and waveform timeline lanes, colored review-note keys, and independent frame bookmarks.
- **File → Open Recent** now records genuine file chooser, Import and viewer
  drag/drop actions, not the temporary media loaded internally by playlist,
  scene restoration or automated QA. Recent history is synced from the
  existing user settings before showing its menu. The Maya regression runner
  writes a disposable isolated preferences file instead of polluting artist
  history with temporary `fr-*` EXR/PNG fixtures. Existing real preferences,
  sessions, playback settings and manually opened files are preserved. A new
  persistent explicit-user-open marker distinguishes even genuine artist files
  in temporary folders from historical QA pollution: **only vanished, unmarked
  FrameReviewer test fixtures** are removed during Recent menu refresh. A
  genuinely opened but currently unavailable network/removable file remains
  visible, disabled and correctly ordered, without a misleading modal or
  silent removal; remounting restores its normal action.
- A live Playlist sidebar tool (toolbar rail, shortcut `P`) with Sequence, Grid,
  Difference, and Inverted Difference playback. The loaded file is always the
  active default item without consuming selection; selected rows are composed,
  or all rows when selection is empty. Sequence plays movies and image
  sequences consecutively on one timeline, while Grid/Difference decode all
  members together through the software compositor (NumPy when available,
  with a Qt fallback). No separate Compose action is required.
- Source audio layout reporting (mono, stereo, surround channel layout, sample
  rate, codec metadata) while preserving the source layout through Qt
  Multimedia. The compact timeline waveform is intentionally a mono peak
  visualization; it does not change playback channels. It now probes the
  *chosen* audio ordinal (not a container stream ID), or the Source's attached
  external audio, and places signed audio delay onto video time. Cache keys
  separate tracks/offsets; route changes invalidate waveform only for genuine
  audio edits. Rectification at native sample rate before decimation prevents
  valid high-frequency sound appearing as an empty waveform. The visualized
  track is not evidence that Maya Qt5 can audition a non-default track.
- **Playback → Audio → Select Embedded Audio Track…** probes the source's
  actual audio streams, distinguishing each Qt audio-track ordinal from its
  FFprobe container stream ID. The artist's selection is stored per logical
  Source, is not rewritten on an unchanged selection and can feed the frozen
  managed Display MP4 audio delivery plan. Maya's Qt5 playback backend cannot
  reliably audition non-default tracks; the export validates the selected
  original embedded stream independently instead of implying it was heard.
- **Playback → Audio → Routing & Sync Diagnostics…** shows the requested
  source/stream/delay versus effective backend capabilities, active track,
  audio-clock validity/stalls and measurable drift. Signed audio delays now
  force the existing separate FFmpeg video + Qt soundtrack path when native
  Qt video/audio cannot offset the embedded soundtrack; changing the route
  preserves playhead, marked range, loop and playback intent. Positive delays
  remain held after loop wrap, and exact rational frame-to-audio milliseconds
  are used for supported fallback seeks. The report is read-only and does not
  dirty a session. Qt5 cannot independently audition arbitrary embedded audio
  streams or perform channel matrix DSP; physical device latency and speaker
  routing are not calibrated or claimed sample-accurate.
- **Opt-in record soundtrack delivery (v4 SEQUENCE/EDL/OTIO):** The Export
  panel offers **Include verified record PCM soundtrack** only for eligible
  Working EXR/Display PNG16 hard-cut image-sequence deliveries. An artist must
  affirm frozen per-occurrence selected embedded audio, an independently proven
  zero-origin external PCM WAV, or explicit image-edit silence. The optional
  **Audio per edit…** selector allows repeated cuts of the same Source to use
  different soundtracks without mutating playback audio or autosaving a review;
  its choices expire on edit-list changes. The single durable queue stages and atomically
  publishes `record_audio.wav` *inside the same output sequence directory*;
  audio is never a loose unjournaled sibling. Supported inputs currently
  require genuinely verified zero-origin **48 kHz stereo PCM s16**, matching
  CFR video and exact `24000/1001` record FPS with 1:1 cuts (2,002 samples per
  record frame). A repeated logical Source has separate editorial occurrences;
  unfilled record gaps become exact zero PCM, and trim in/out maps to the actual movie
  decoder index or source-local image frame index. External files must be
  canonical RIFF/WAV with independently proven exact zero start, sample count,
  format and SHA256; arbitrary sound files, WAV metadata chunks and ambiguous
  path aliases do not become synchronized merely because the artist selects
  them. Output WAV samples are independently redecoded and compared
  to selected original sound, SHA256 and source hashes are checked, and the
  queue's cancellation/backup/rollback/recovery covers audio *with* the image
  frames. The original **Audio-only record layer…** dialog preserves its
  backwards-compatible ONE external canonical PCM WAV bed. The additional
  **Manage ordered audio-only layers…** Maya editor explicitly authors **one
  to three** independently proven external PCM WAV occurrences, each with its
  own record start, source in-frame, duration and exact mute/half/unity gain.
  Audio-only layers overlap selected per-cut sound and one another at exact
  2,002-sample record-frame boundaries. Authored table order is meaningful:
  apply signed PCM16 gain with truncation toward zero and *saturate after each
  layer*, rather than treating the tracks as a commutative floating mix.
  An explicitly positioned layer can sound through an otherwise silent picture
  gap. The same independently proven mixed PCM drives read-only source-labeled
  waveform bins, video-master Qt audition, WAV and optional MOV/NUT; export-only
  edits never autosave, while explicit Remember stores v2 exact-edit-fingerprint
  Session intent. Legacy saved single-layer v1 intent remains readable.
  Changing video edits invalidates saved intent, and EDL/OTIO requires
  informed VIDEO-only lossy consent because these audio-only occurrences and
  gains cannot be faithfully round-tripped. More than three beds, dynamic
  fades, arbitrary sub-frame offsets, surround/DSP/retime and physical speaker
  synchronization remain unsupported.
  An independent second opt-in **verified SDR RGB/PCM record MOV**
  is available ONLY together with Display PNG16/WAV delivery: one lossless
  H.264 RGB 8-bit black-matted SDR derivative plus exact uncompressed PCM
  `record_av.mov` is published inside the *same* durable directory. Each
  decoded movie RGB pixel is compared against the independently decoded
  staged Display PNG16 frame, with explicit 16→8 quantization and black matte;
  decoded MOV PCM samples, exact rational frame/audio timestamps and all
  committed receipts are compared against original chosen sources and WAV.
  **This MOV is neither HDR/float/16-bit nor alpha-preserving.** A complementary
  explicit **lossless RGBA16/PCM
  record NUT** option publishes `record_rgba16.nut` in the *same* atomic Display
  PNG16/WAV directory (and can coexist with the SDR MOV). FFV1 `gbrap16le`
  preserves every Display SDR 16-bit straight RGBA component and transparent
  alpha, and 48 kHz stereo PCM is bit-exact; NUT carries an exact 1/48000
  stream time base with video PTS `0,2002,4004,…` at `24000/1001`.
  Independent FFmpeg/FFprobe redecoding checks the original frozen source
  SHA256, every PNG16/reference channel, PCM sample, stream identity, PTS,
  staged receipt and recovered output. It is **not scene-linear/HDR video**:
  keep Working float EXR for negative/over-one native scene values. NUT player
  compatibility is more limited than MOV/MP4; there is no standalone mux queue.
  No standalone video publish, AAC, arbitrary external offsets, MP3 input, respeed, crossfade
  or unverified hold is implied; these combinations fail closed. Audio remains dropped
  unless explicitly opted in and confirmed before queueing.
- **Record-timeline viewer and verified sound inspection:** Imported/repeated
  SEQUENCE/EDL/OTIO edit occurrences now have read-only, exact record-frame
  identity in the primary Maya viewer: logical Source ID, occurrence position,
  zero-based movie decoder index and original signed image frame label are
  distinct. Gaps, held trims, authored holes and decoder errors have explicit
  states; rapid shutdown/reload cannot reuse stale frame callbacks or clamp an
  out-of-range record frame to the last image. **Verify record waveform…**
  independently reconstructs the same frozen embedded/external PCM selected
  for v4 export on a cancellable Qt worker and plots one *unsmoothed* amplitude
  bin per exact `24000/1001` record frame. Hard-cut and silent-gap samples are
  not blurred into each other. The waveform and frame tooltip follow the shared
  record playhead. The separate opt-in **Audition verified record audio…**
  reconstructs the exact bounded PCM on a cancellable Qt worker and uses the
  *one existing video playhead* to seek a Qt5 48 kHz stereo PCM16 audio output
  buffer at verified frame/sample boundaries. Pause, scrub, seek, looping,
  volume, mute and shutdown invalidate stale sound instead of switching to the
  wrong edit source. The actual local device must support the format; physical
  loudspeaker audibility, latency and sample-accurate device synchronization
  are NOT proven by Maya headless testing. Explicit **Remember these per-edit sound choices in the review
  Session** saves typed occurrence-specific editorial intent only on a real
  user edit; reopens and relinks still independently re-prove source audio and
  reject mismatched edit fingerprints rather than inheriting sound from another
  occurrence. Default export-only choices never autosave. A viewer record gap
  is opaque black, whereas v4 exported RGBA gaps are transparent black.
- **EDL/OTIO soundtrack provenance guard:** A plain CMX3600 video cut list
  or current OTIO interchange track cannot encode FrameReviewer's separate
  selected embedded/external audio per occurrence. File → Export Timeline now
  identifies this as a *lossy audio-intent omission requiring explicit consent*;
  an edit list whose saved audio fingerprint no longer matches its cuts is
  rejected. Portable `.frsession` and the independently verified record
  WAV/Display MOV are the authoritative preservation routes. An external WAV
  path is never silently leaked into an OTIO vendor field as fake synchronized
  sound, and transitions/retimes still cannot be exported as lossless cuts.
- Review drawing tools (brush, eraser, rectangle, ellipse, arrow, and text), undo/redo, import/export of note JSON. Text is edited directly over the image: click to type, click an existing text object with the Text tool to edit it, use Ctrl+Enter or click away to commit, and Escape to cancel.
- Notes Select tool: click or Shift-click individual strokes, drag a marquee to select multiple strokes, move by dragging the selection, scale using four corner handles (Shift locks the aspect ratio), and rotate using the top circular handle (Shift snaps to 15°). Delete/Backspace removes selected strokes; Escape deselects. Transforms modify stroke coordinates and participate in note undo/redo.
- Text annotations are first-class selectable note objects. They can be moved, scaled, rotated, deleted, and erased as a whole; legacy position-only text notes are upgraded to object geometry when loaded.
- Onion skin for annotated keyframes, with independent previous/next depth (0–20), opacity (0–100%), optional distance falloff, warm/cool directional tinting, and persistent controls under Properties → Draw. Ghost strokes never modify exported notes.
- **File → Open Media or Review…** (Ctrl+O) opens media or a FrameReviewer notes JSON, resolving the associated source clip and loading its annotations.
- **Tools → Maya Integration → Playblast Current Shot** captures the current Maya Camera Sequencer shot (or the playback range when no shot is active) as a temporary JPEG image sequence and immediately loads it.
- **Maya Sequencer playlist playblasts** reuse immutable cached image sequences
  only when the scene is saved, unchanged and the original shot signature is
  still current. Changes to a Maya scene after inventory are rechecked before
  rendering; dirty or untitled scenes produce fresh private cache revisions
  without replacing previously reviewed output, and a newly saved scene must
  be re-inventoried. Cache manifests now freeze bounded counts, exact filenames,
  byte sizes and per-frame SHA256 hashes. Reuse verifies **every** frame,
  rejecting symlinks, missing or altered middle frames, injected files and
  legacy filename-only manifests that cannot prove original content. Damaged
  revisions are retained for investigation; replacement renders use a new
  private directory and never overwrite previously reviewed files. This full
  recheck adds hashing I/O on reuse and is not a substitute for workstation
  or network filesystem stress tests. Save the scene for repeatable cache
  reuse; shot UUIDs and source-owned notes remain stable across revisions.
- Raster PNG icons bundled alongside the original SVGs for reliable rendering across Maya/Qt installations.
- Crop controls with horizontal/vertical flips and adjustable output resolution.
- OpenCV-powered crop tracking: position (1 point), rotation/scale (2 points), affine (3 points), and perspective using at least four image features. Tracks include filtering of bad matches and optional rectification on export. Signed/negative sparse sequences now pass explicit source-frame identities to the actual Maya tracking worker: it preflights every requested frame, refuses authored gaps or missing/corrupt-source risk **before spawning the Qt thread**, and caps one expensive tracking pass at 2,400 frames. No motion interpolation is fabricated for missing plates. Live Maya range refusal and actual OpenCV decoded-source pixel selection have regression coverage; advanced bidirectional tracking and unrestricted managed stabilized export remain pending.
- Tracked crops now persist per logical Source in `.frsession` as bounded,
  strictly validated **original signed source-frame keys** with native raster,
  exact FPS rational, flip orientation, algorithm, transform mode, centers,
  quads, manual offset and observed per-frame quality. Restore never maps a
  missing source key to the nearest frame or reuses wrong geometry after relink.
  Maya **Crop/Track** reports actual feature/inlier counts, forward/back error,
  reprojection error, lost frames and a bounded *heuristic* confidence (not a
  statistical probability). Lost motion holds the last real pose and blocks
  affected-range export rather than fabricating a valid track. **Correct
  Current Frame** bakes one translation-only crop offset into the selected
  source key as a manual correction with Undo/Redo; no unmeasured intervening
  motion is interpolated. B-frame CFR movie tracking uses proven accurate
  FFmpeg source-index seek. Reverse/bidirectional tracking, general stabilization,
  perspective manual editing and full production GUI/GPU validation remain open.
- **Crop/Track → Stabilize View (preview only)** offers a genuinely integrated,
  opt-in native Qt view stabilization for bounded, verified translation-only
  tracks. Its immutable CPU model uses original signed source-frame labels,
  frozen source identity, exact FPS, PAR/orientation and measured LK quality.
  The QGraphicsView camera pans the *entire* native scene — plate, annotations,
  crop, selection and pixel probes — together, so no annotation positions,
  native EXR precision or persisted image pixels are silently rewritten.
  Enabling refuses gaps, lost/held/manual/weak tracks, re-linked mismatches,
  unsupported affine/perspective motion and excessive shifts; navigation uses
  the same stable source anchor without autosaving. Source-owned opt-in/anchor
  persist in the session; geometry/track edits disable a stale preview.
  **Baked stabilization is separately opt-in on Export Media** and supports
  only original square-PAR, full-window, canonical HALF/FLOAT EXR image sequences
  with declared linear-to-linear, ungraded color, verified translation tracks,
  and a bounded marked range (up to 60 frames) containing the original anchor.
  The same Source, original signed frame identities and fixed preview anchor
  are frozen before the user confirms Working float32 RGBA EXR delivery. Every
  source EXR and tracking record is SHA256 frozen; each native float frame is
  bilinear-translated in scene-linear light and regions with no actual source
  pixels have truly transparent alpha, not clamped or fabricated border color.
  The existing single journalled queue independently redecodes every output
  frame to check signed source/frame/offset identity, native negative/over-one
  float samples and zero-alpha borders at stage, promotion and recovery. Lossy
  Display PNG, windowed/multipart/rotated/non-square EXR, unverified tracking,
  MOV/MP4, arbitrary crop/output resize, annotation burn-in and audio remain
  unsupported in this baked path. The **Qt preview** is SDR and does not imply
  calibrated hardware HDR/GPU/OCIO parity.
- UI controls for Toolbar, Properties, Timeline, Playback Controls, and Status Bar visibility, remembered per user.
- The dockable **Properties** sidebar groups tools by workflow: Media/Playlist,
  Draw/Review, Crop/Track, and Export/Flow. Each page has a contextual title,
  shortcut, scrollable sections and persistent advanced-section expansion. In
  Draw, annotation keys and navigation precede advanced pen settings; Review
  exposes Color Correction, source-owned Color Pipeline and Image Scopes without opening overlay options.
  Playlist shows compact source rows with active/offline state, versions,
  frame rates, tags and an explicit comparison section. Narrow panels keep
  core controls accessible instead of clipping tool buttons.
- The scoped sidebar palette now follows Maya's native grey controls and
  selection accent rather than introducing a second branded UI. The menu bar
  groups File/session I/O, Edit annotations, Playback transport/ranges/audio,
  View panels/playlist/overlays/inspection and Tools/annotation/reference/Maya
  integration. Frequent J/K/L transport and editing actions are immediately
  visible; Playback Speed is a one-level menu, never a nested flyout.
  Existing shortcuts remain usable in the docked panel.
- Optional Flow Production Tracking / ShotGrid integration when available in your studio environment.

### Session-first studio review (in development)

- **File → Open Session / Save Session / Save Session As** reads/writes portable
  `.frsession` JSON. Live source IDs keep notes, markers, source-frame ranges,
  flips, crops, media representations, and comparison state attached to the
  reviewed source. Saves are atomic and preserve unrecognized session fields.
  A separate `.autosave.frsession` or `~/.framereviewer/recovery/` checkpoint is
  maintained while the session is dirty; a checkpoint never silently overwrites
  an explicitly saved review. Unchanged sessions are not rewritten on timer,
  and playhead navigation does not count as a review edit. Automatically named
  recovery snapshots are limited to the three newest; explicitly named session
  files and their autosave sidecars are never purged. Maya scene state stores a
  compatible snapshot.
- The Playlist stores stable source identities. Double-click a clip to open it,
  drag rows to reorder, or select **Relink…** to change its representation
  without losing its drawings. Unavailable paths remain in the playlist.
  `FRAMEREVIEWER_PATH_MAP` remaps studio mount locations on another machine.
- Comparison modes include Sequence, Grid, Difference, Inverted Difference,
  A/B, Flicker, Blend, Wipe, Split, Checker, and Over. Select a pair for A/B
  modes; extra selected sources are ignored in those two-input comparisons.
  Controls include active A/B image, blend/wipe position, orientation,
  checker size, and flicker duration. Comparison changes reuse resident member
  decoders where supported. Grid renders each source stretched to its assigned
  cell with transparency preserved, including empty tiles in an incomplete row.
- **J / K / L** shuttle backward, pause, and forward, with multiple-speed
  presses. Choose Once, Loop, or Ping-Pong transport. Reverse playback uses the
  FFmpeg frames backend and mutes reverse audio until scrub audio is supported.
  In Real-Time mode the audio clock continues across a video cache miss; in
  Play All Frames mode video timing owns the clock. Audio-seek coalescing,
  bounded drift correction and soundtrack end handling reduce needless
  QtMultimedia decoder flushes, but actual studio audio devices and codecs
  require interactive Maya verification. Audio-master playback now detects
  stationary Qt audio progress and temporarily uses the existing video clock
  rather than freezing the playhead. A bounded audio recovery correction avoids
  rewinding video, terminal soundtrack events cannot repeatedly restart audio,
  and pending seek acknowledgment suppresses redundant decoder flushes. Audio
  diagnostics report clock status; tests are headless, not speaker hardware.
- **Review → Image Processing → Color Pipeline…** (also View → Image Inspection)
  edits the logical source's input/working/display spaces and optional OCIO
  config/display/view/look; no-op changes do not dirty the session. Precision
  EXR pixels are transformed in floating point from source through working to
  display **before** conversion into the main QGraphicsView's SDR pixmap.
  The legacy exposure/grade is applied once to native precision where possible,
  instead of merely grading the previously quantized preview. On unsupported
  dependencies, the viewer declares its unmanaged/failed fallback status.
  A missing native Python OpenEXR module no longer automatically disables
  precision EXR review: FFmpeg/FFprobe plus NumPy can decode FLOAT32 RGB(A)
  raster pixels, including signed highlights and transparency, directly into
  the same immutable primary viewer payload. A separate bounded native CPU
  scanline reader now preserves original HALF/FLOAT/UINT samples and signed
  EXR data/display windows for independently verifiable single-part RGB(A)
  NONE/ZIPS/ZIP scanline files without Python OpenEXR. Absolute scanline chunk
  positions and channel bytes are checked instead of guessing where FFmpeg
  placed nonzero-origin pixels. Arbitrary multipart/AOV/deep/tiled/PIZ files
  still require additional native handling; FFmpeg float fallback remains
  confined to canonical windows and may be slower during scrubbing.
  **Display Pixel Inspector** reports both precise source RGBA (including
  negatives and values above one, dtype, source frame and alpha mode) and the
  monitor-referred SDR sample. **View → Image Inspection → Pin Pixel Sample A/B**
  holds two image-normalized positions through zoom, flip and frame changes.
  Both pinned markers update from the presented frame and show signed RGB A−B
  deltas in matching source/working space, otherwise 8-bit display space.
  Clear Pinned Samples removes them; failed frames cannot reuse stale HDR
  samples, and inspection does not change review/autosave state. This is not
  a native HDR monitor framebuffer;
  actual OpenGL context and real studio OCIO configuration still need tests.
  **View → Image Inspection → Native-aware Pixel Magnifier** adds a bounded,
  read-only 9×9 nearest-neighbor Display pixel grid on the actual primary Maya
  image viewport, with a mouse-transparent floating readout. Pixel addressing
  follows existing PAR, quarter-turns, flips, panning, zoom and pinned A/B
  source-normalized geometry, never annotation/crop artwork. The tooltip
  separately reports measured original HALF/FLOAT/UINT storage samples,
  source/stage/alpha and signed frame identity when an immutable native payload
  exists. EXR displayWindow padding and unavailable/decode-error frames never
  invent native HDR measurements; hover generation is invalidated on new frames
  and closure. The magnifier is not a GPU readback, hardware HDR monitor or
  Session edit, and makes no review autosaves.
- In native precision comparisons, hovering the actual displayed image and
  using the A/B pinned inspector can also report each contributing member's
  **original ungraded** float RGBA, logical source ID, signed source frame,
  pixel location and input color interpretation. Grid/Split coordinates are
  mapped to the correct contributing tile or half; Qt-only members are labeled
  display-only instead of acquiring invented HDR values. The scopes dialog
  can select individual members at Source or pre-composition Working stage;
  a member's nonexistent independent Display output is explicitly unavailable.
  **Visible-pixel provenance** now selects contributors by the exact composed
  raster's pixel-center ownership masks: A/B and Flicker show the selected
  source, Wipe/Checker/Split/Grid show their actual cell/side, while Blend,
  Difference and Over report genuinely contributing original members. EXR
  canvas padding, intentional missing frames and decoder failures do not yield
  invented native HDR samples. Source/occurrence IDs, source-native integer/
  float storage and signed source frames stay distinct from the displayed
  working-light composite. During asynchronous seeks, readouts are fenced by
  Player's actual `framePresented` source/QPixmap receipt, not the transport
  playhead alone; a same-frame comparison mode redraw invalidates prior samples.
  Sparse **Hold** reports the requested absent signed source label separately
  from the true earlier source frame; native source values are shown only if
  that previous real frame's immutable payload remains in the bounded cache.
  Evicted Hold frames and Black/Checker/Error placeholders never invent native
  precision. Explicit Source-specific straight/premultiplied alpha declarations
  reach sequence decoding and changing one invalidates old source/comparison
  caches; EXR channel count alone does not prove alpha association.
  Direct single-Source loads always clear obsolete comparison export intent,
  even when the plate was a member of that prior composition.
- **Precision comparison → managed color delivery:** bounded CPU compositing
  now aligns resampled source pixels by centers, maps each member's native PAR
  coverage into the first member's physical reference field without stretching
  transparent outside edges, converts Source to shared Working *before* the one
  required resize and applies the common grade once. Grid deliberately scales
  each source into its actual cell (no redundant full-canvas pre-resize).
  Original negative/over-one RGB, fractional alpha and independently sourced
  input color remain in Working float EXR. Display PNG16 alone quantizes and
  applies display sRGB, never mislabeled native HDR. Real Maya2024 native EXR
  source/viewer → seek/compare/inspector → Export UI → ONE durable queue →
  independently decoded float EXR and SDR PNG16 tested distinct signed RGB,
  alpha, pixel aspect and original frame labels. **Unequal-PAR A/B/Flicker**
  now display the selected native plate in the same first-Source physical
  square-pixel reference field as native managed v2 EXR/PNG16 output. The
  original signed HALF/FLOAT/UINT member sample remains independently
  inspectable: transparent regions outside the selected member's physical
  coverage are NOT fictional source pixels. The supported current frame must
  actually be presented as full-reference Working float, with a matching
  source/frame/QPixmap receipt, before nonidentical-geometry A/B or Flicker can enter
  the ONE durable export queue. Identical-native-geometry A/B, concatenation and Qt-only
  fallback retain their original contracts; unproven/evicted, stale or
  downsampled selected frames fail export preflight. Real Maya2024 primary
  A/B source B preview, native inspector and independently decoded managed
  24x8 Working float EXR matched pixel for pixel including fractional alpha,
  source-negative RGB and physical edge transparency. GRID→A/B warm mode
  switches also restore the reference PAR. Native EXR v2 Flicker delivery
  snapshots its exact `flicker_frames` period (1–120 frames) and selects each
  output member from the **absolute session frame**: a two-frame real Maya
  A/B alternating preview matched both independently decoded managed Working
  float EXR frames, not a repeated source A. Legacy square-pixel v1 and mixed
  movie v3 Flicker exports remain unsupported (fail closed). Source dimensions
  matter as much as PAR: identical `3/2` ratios with different native widths
  remain nonidentical physical fields, so selected A/B/Flicker managed exports
  now rectify from the *real* original pixels into a centred square-pixel
  reference field; shorter members have transparent outside boundaries, and
  larger plates are physically centre-cropped, never stretched. Native EXR v2
  comparisons with different source width/height automatically freeze and
  validate each member's real physical geometry even for PAR1 inputs. The
  actual Maya primary selected-B 16×8 vs 8×8 (both PAR3/2) preview matched
  independently decoded Working float EXR and Display 16-bit sRGB PNG in
  the SAME durable queue, including negative channel clipping, blue sRGB
  .25→.537, source signed frame −2 and half alpha. A real A/B Session save/
  reopen with B8×4 preserved its stable logical source identities, selection,
  original negative frame, original pixel sample and first-reference 24×8
  Working float extent. Rotated, flipped or cropped composition presentation
  remains viewer-correct but **managed composite** export rejects those
  unimplemented effects before enqueue; single-source verified geometry
  output has different capabilities. Unpresented/Qt-only/stale/evicted or
  downsampled selected previews still reject, never publish approximate pixels.
  GRID always uses a square-pixel stretch canvas, even when all members share
  the same non-square PAR. Both safeguards have real Maya offscreen regressions.
  No GPU/HDR monitor parity claimed.
- **View → Image Inspection → Select EXR Part / Layer…** permits explicit
  native OpenEXR RGB(A) channel selection on a single EXR sequence. Selection
  follows the existing logical Source through session recovery and reload;
  selecting the same channels is a no-op. Unsupported ambiguity, multipart
  geometry and missing native OpenEXR APIs are reported rather than silently
  selecting a random layer. FFmpeg floating-point fallback is intentionally
  limited to canonical full-frame zero-origin single-layer raster EXR; it
  does not preserve displaced/cropped data windows or arbitrary UINT/layers.
- On a verified native EXR, the primary Maya Source decoder places original
  signed dataWindow pixels into the displayWindow-sized precision canvas.
  Original absolute EXR coordinates, selected channel/part identity, negative
  frame numbers, HALF/FLOAT/UINT sample dtype and PAR are retained, while
  displayWindow padding has transparent black pixels rather than recycled
  decoder memory. The pixel inspector reports absolute EXR coordinates and
  explicitly labels padding as **no original native sample**, not measured
  HDR. Scene annotations/crop remain displayWindow-local so source and
  overlay alignment survives zoom, flip and rotation. Supported signed/offset
  windows are CPU-decoded and verified before the Maya probe authorizes their
  canvas, even without Python OpenEXR. Invalid chunk coordinates fail closed;
  FFmpeg is never trusted to establish nonzero-origin placement. Managed
  Export still rejects these noncanonical windows pending separate proof.
- Per-frame native EXR dataWindows may shift or change size while keeping a
  constant display canvas. The actual SequenceSource accepts these legitimate
  changes but rejects a different absolute displayWindow or PAR as a geometry
  decode error, not an intentional missing frame.
- **Intentional missing frame presentation and Display export fidelity:**
  Black/Checker/Error placeholders now use ONE bounded deterministic opaque
  RGB(A)8 reference in both Maya's actual SequenceSource preview and the managed
  single-source Display PNG16 writer, whose 16-bit channel integers are exact
  `8-bit channel × 257`. Checker cells share identical native-pixel sizing;
  Error uses an unambiguous red header/geometric X rather than platform-dependent
  text glyph pixels. Signed missing source labels, Hold origins, distinct failed
  real-file decodes and Source IDs remain in frame_status and output manifests,
  not embedded as false source measurements. Actual Maya import/seek/Export UI
  through the existing durable queue was compared against every independently
  decoded PNG16 checker RGB/alpha component and signed frame map. The byte-level
  guarantee applies to native-size, unscaled **single-source Display PNG16**
  holes; mixed-color compositing, transformed geometry, Working float EXR and
  Qt-downscaled placeholders do not claim direct pixel parity or HDR content.
- HDR-aware comparison now operates on available native floating-point member
  pixels in the shared **working** space before producing the SDR result.
  Each logical source can have a distinct input interpretation, while the
  common grade and display transform are applied once. Grid preserves
  transparent stretch-to-fit cells; A/B, flicker and concatenation retain
  source frame identity, including frames repeated under Hold. Mixed 8-bit
  members and absent NumPy/OCIO have explicitly labeled fallback behavior.
- In live precision comparison, members with different pixel aspects or
  physical image extents now project into the first member's common physical
  reference field. Overlap modes retain actual native RGB/alpha without
  inventing stretched edge pixels; out-of-coverage pixels are transparent,
  and original-member inspection uses the same invertible mapping. The
  intentional full-cell stretch in Grid is unchanged; equal-PAR A/B and
  concatenated source playback preserve their original native representation.
  Unequal-PAR selected A/B/Flicker now rectifies to the native reference field
  only when precision is available and visibly confirmed. Managed export
  rejects stale, unsupported or Qt-only selected presentations rather than
  publishing pixels inconsistent with the current Maya viewer.
  The Maya overlap viewport uses the reference source's correct PAR; mixed-PAR
  Grid deliberately displays a square-pixel cell montage.
- **Generation-safe primary frame presentation:** frame decode and original
  precision payload now form one source/frame/request-generation transaction
  in the actual `Player`. A synchronous cache notification, latest seek,
  replacement of an active composition, queued FramePump signal or close may
  invalidate the old source *during* `get()` or `get_payload()`: the returned
  obsolete QPixmap or float sample cannot overwrite the newer Maya surface or
  emit an invalid `framePresented` receipt. Composite reloading emits the
  initial frameChanged event for the new source even if the old timeline was
  also at frame zero; Qt navigation cannot silence that source identity
  transition. Sparse `Hold` origin lookup uses a bounded sorted-frame search,
  and closed sequence sources cannot re-decode evicted frames. Actual Maya2024
  FFmpeg-generated EXR and Qt reentrant/queued-seek/source-shutdown tests cover
  stale-source rejection, original signed frame labels and decoder errors,
  without changing the audio-master clock or autosave behavior. Source-owned
  **revision fences** now additionally reject reentrant shutdown, source
  replacement, color changes, missing-frame policy switches and listener
  callbacks occurring *during* native EXR read, SHA proof, float render or
  QPixmap cache publication. Closing cannot resurrect native payloads or emit
  a late `frameAvailable` receipt. Signed sparse 100,081-frame timelines
  switch policies by visiting resident cache rather than allocating/scanning
  every gap, and enormous synthetic Black/Checker/Error cards are raster/
  memory-bounded Qt-only placeholders, never fabricated Source HDR pixels.
- **Source mutation-aware selected-source delivery:** an opt-in, bounded
  512 MiB per-original-file SHA256 reader records *before and after* decoding
  the unchanged real EXR inode/path and retains provenance only with the
  original native FramePayload in the bounded cache. Real Maya A/B/Flicker
  enables that trust mode when initially selected or upon warm Blend/Grid→A/B
  switching; old no-hash pixmaps are discarded, never back-certified. The
  reviewed current native source ID, actual signed source frame, original
  file path and SHA must remain intact when requesting managed physical-field
  delivery. Replaced/modified originals (including same-size files with
  restored mtime) invalidate cached Source pixels and **block that entire
  selected comparison's export until an explicit reload and new frame
  presentation**. Hold verifies its actual previous stored Source, never the
  missing signed label; Black/Checker/Error and evicted payloads have no
  native proof. Unverified movie/Qt-only modes retain truthful fail-closed
  semantics. SHA checking never runs in the default normal Sequence playback
  path; this is CPU validation, not an HDR GPU claim. The durable queue still
  independently freezes and checks its own per-source media receipts; it does
  not replace the viewer's signed original provenance.
- **View → Image Inspection → Image Scopes** opens asynchronous RGB/luminance histograms, a
  luminance waveform and Rec.709 vectorscope. Sampling is downsampled and
  generation-cancelled so successive scrubs cannot draw out-of-date results.
  The viewer refreshes scopes after a decoded frame reaches the presentation
  surface, not merely when the playhead requests a different frame. Its
  **Pixel stage** selector now scopes original source float/HDR pixels,
  transformed working float/HDR pixels, or the actual displayed SDR pixmap.
  **EXR coverage** can select the full display canvas or actual native
  dataWindow intersection for Source/Working scopes; Display always samples
  the complete SDR viewer. This keeps intentional zero-alpha EXR padding from
  silently diluting highlight/negative diagnostics.
  Unavailable source/working precision (including working-stage composites
  which cannot be reversed to an original source) is explicitly reported,
  never reconstructed falsely from an 8-bit image. Real FFmpeg EXR source,
  working and display histograms are tested through the actual Maya window.
- The separate **experimental GPU multi-viewer** remains off by default:
  `GPUMultiViewer` can pass CPU-color-correct float Display pixels into
  RGBA32F GL textures if an existing valid Qt OpenGL context actually
  supports it. Shader mode, texture upload and context failures explicitly
  fall back to the CPU SDR reference; no GL library installation is treated
  as evidence of hardware HDR/OCIO GPU parity. The primary annotation
  viewport is unchanged pending physical workstation color/framebuffer tests.
- **View → Rotate View 90° Clockwise / Counterclockwise / Reset** applies a
  single pixel-aspect-aware, quarter-turn affine transform to the actual
  QGraphicsView scene, including annotations, selection, crop, onion skin,
  pixel probes and magnification. Native source image coordinates remain
  unmodified. The current source's display rotation is saved in its review
  session; selecting the same rotation creates no autosave. Media PAR is taken
  from the probed source. The current export path rejects non-square-pixel
  or rotated presentation instead of silently delivering wrong framing;
  per-source PAR-mismatched comparison geometry and rotated exports remain
  explicit production limitations, not encoded output transforms.
- **Timeline** accepts mouse-wheel zoom, Shift+wheel pan, and a right-click
  menu with Fit Session / Fit Marked Range. Navigating a time window does not
  alter the source frame or mark positions.
- **File → Timeline Interchange → Import / Export EDL / OTIO** converts edit lists through
  the portable source/session model, recording rational frame rates, source and
  record trims, clip IDs and unsupported edits as explicit notices. OTIO needs
  an optional `opentimelineio` installation; CMX3600 EDL works without it.
  Imported record positions, gaps, rational retimes, and freezes feed one
  shared playhead immediately after import and on session reopen. Separate
  edit occurrences keep their source IDs even when referencing the same file.
  Offline imported reels preserve the record edit and resume composition once
  the missing media is relinked. Movie timecode origins are aligned to local decoder indices
  when the source contains a timecode tag; missing or invalid origins are
  reported, because the correct source edit in-point cannot be inferred.
- **Flow / ShotGrid** credentials can come from an authenticated Toolkit
  engine, `FRAMEREVIEWER_SHOTGRID_SITE`, `FRAMEREVIEWER_SHOTGRID_SCRIPT_NAME`,
  and `FRAMEREVIEWER_SHOTGRID_API_KEY`, or a per-session login dialog.
  Plaintext script API keys are never written to QSettings; old saved keys are
  discarded during panel setup.
- Flow publication now also guards stale UI callbacks: switching Version,
  local Source or disconnecting while a queued publish is in flight cannot
  reload notes for the wrong selection. Confirmation remains mandatory and
  interrupted remote writes must be reconciled on reconnect; fake-client Qt
  tests do not replace a real ShotGrid server permissions check.
- **Draw/Review typed records:** the Review sidebar contains a source-owned
  record editor with comment, category, priority, status and layer controls.
  Its Discussion section displays existing replies as plain text and allows
  adding local replies without remote publication; unfinished reply drafts
  survive selecting other notes within the source, and locked layers reject
  changes without clearing the draft. Adding a reply does not silently apply
  other unfinished note edits.
  Select, marquee, move, scale, rotate and onion-skin remain the established
  PlaybackSurface interaction path, now projected through stable `ReviewNote`
  and shape IDs. Layer hide/lock guards viewport edits; source-frame keys,
  replies, attachments and unknown fields survive `.frsession`/JSON imports.
  Duplicate imported IDs receive derived identities rather than replacing
  local edits. Review → Attachments can now **Browse / Add** a bounded local
  file or verified still to an existing note, and **Remove** its unsent local
  reference. This requires first saving a named `.frsession`: files are copied
  into a source-scoped `.framereviewer-assets` directory beside the session,
  using exclusive, no-symlink local access, content hashes and portable
  relative URIs. The original remains unchanged; removing the reference
  retains its managed copy for undo/recovery. **Save Session As** to a different
  local directory verifies and copies trusted managed assets first, then
  atomically writes the new session JSON; a failed asset copy/verification
  rolls back only newly created files and preserves the original session.
  Matching existing target assets are reused without a rewrite; collisions,
  symlinks and untrusted relative asset references reject safely. Relocation
  is bounded to 256 references and 256 MiB total. If moving a session manually
  outside FrameReviewer, move its `.framereviewer-assets` folder too.
  Synced/remote assets, stale revisions, locks and conflicts are
  read-only; there is **no automatic ShotGrid attachment publication**.
- **Flow / ShotGrid publication:** loading a remote Version now attaches its
  verified Version/project/shot/site IDs to the logical source. The Flow tool
  can explicitly queue selected typed notes or Version statuses in a private,
  durable per-site offline outbox. A second confirmation is required before
  the background worker publishes. Bounded retries check the server for an
  existing marker, guard status changes against newer remote values and retain
  conflicts. Acknowledged notes become remotely linked and clean **only if**
  their current content still matches the published revision; changed local
  records stay dirty. No API keys are saved; actual studio server/permissions
  remain to be validated.
- **Flow → Inspect Conflict…** is an opt-in recovery action for conflicted
  offline publications. A background read fetches the current scoped remote
  Version status or tagged Note and shows the local versus remote evidence.
  Safe choices are acknowledge already applied, requeue after verification,
  rebase a Version status against the actual server value, or explicitly discard
  the local job. Each decision is confirmed and rechecks remote state and local
  revision; it makes **no remote changes**. Notes with divergent content or
  duplicate tags cannot silently overwrite. A later **Sync Queued Changes**
  still requires its own consent. Real studio permissions remain untested.
- The Export sidebar selects **Source / Working / Display** output. Source
  retains the legacy FFmpeg route by default. For supported image-sequence media, Working
  produces floating-point EXR and Display produces SDR movie or image-sequence
  output using the declared precision source→working→display CPU pipeline.
  Color stages, actual source-frame identity, missing-frame decisions and
  transform/verification evidence are frozen in the queued manifest. Real
  FFmpeg+FFprobe tests verify unclipped negative/HDR working pixels and
  transferred display pixels. Unsupported source/geometry/speed/composite
  combinations fail explicitly; there is no silent relabeling of source-stage
  output as color-managed. Burn-ins on the legacy source route use FFmpeg
  `drawtext` or the existing raster fallback.
- **Verified single-source image geometry delivery:** for supported managed
  Working EXR and Display PNG image-sequence output, pixel aspect ratio,
  integer post-flip crop, horizontal/vertical flips and clockwise 90° turns
  can be explicitly frozen as one sample-domain plan. It uses float working
  pixels to bake square-pixel PAR before display encoding and validates the
  exact output raster. The plan and its signed source-frame map are retained
  in the existing export queue, verified sidecar and crash-recovery checks.
  Geometry is confirmed before output selection; unsupported movie/legacy
  Source/composite formats or noncanonical EXR windows/layers still reject.
- **Managed movie-input exports** (explicit Working/Display mode) support
  bounded constant-frame-rate video with exact zero-based decoder index to
  source-frame mapping and a user-confirmed, immutable color interpretation.
  Working exports float EXR frames; Display exports supported 16-bit PNG or
  SDR MP4. RGB and supported alpha-bearing movie codecs are decoded to float
  before source→working→display transforms. Movie Source-stage output can also
  use the confirmed precision path through the job API; the default Source UI
  remains legacy/unmanaged. Tagged YUV requires a matching confirmed color
  matrix/range; untagged YUV prompts for the missing interpretation. Managed
  movie export defaults to **dropping audio with explicit confirmation**.
  For a single-source, unretimed, zero-origin CFR **Display MP4**, the Export
  sidebar instead offers an explicit **Preserve embedded audio** opt-in for
  one selected stereo 44.1/48 kHz track, sample-trimmed to the chosen frame
  range and delivered as stereo 48 kHz AAC. The frozen track ID, timebase,
  sample alignment, source identity and encoded audio are verified after
  muxing and again during durable queue receipt/recovery. External tracks,
  delays, surround, multiple video streams and nonzero or unknown stream PTS
  origins are rejected; the video-only path is unchanged. VFR,
  unverified source FPS/PTS, complex tracking, burn-ins, speed changes and
  ambiguous color/alpha inputs fail closed. Stage-specific frame maps,
  color provenance and verified pixel formats survive the existing durable
  queue, overwrite rollback and recovery. This is not arbitrary movie/OCIO
  certification or a GPU HDR output pipeline.
- **Managed image-sequence comparison exports** now support the actual
  A/B, Blend, Wipe, Split, Checker, Over, Difference, Inverted Difference and
  Grid workflows when every member is a bounded EXR/PNG/TIFF image sequence.
  The comparison captures each logical Source ID, signed sparse frame map,
  Hold/Black/Checker/Error policy, source FPS, distinct input color space,
  alpha association and shared grade/display transform before dispatch. All
  members are composited in floating-point working light with the same CPU
  pixel reference as the viewer; Working outputs HDR float EXR, and Display
  outputs 16-bit PNG (or verified opaque H.264 MP4). The one durable export
  queue validates exact member provenance on stage-to-output promotion and
  crash recovery, retaining reversible overwrite backups. Export requires
  explicit confirmation that soundtracks will be omitted and does not burn in
  review notes. An opt-in **version-2 native EXR-only** composite freezes
  each member's exact rational pixel aspect, canonical full zero-origin EXR
  windows and dimensions, then uses the same CPU physical-field rectification
  as the live viewer. Overlap modes deliver square-pixel Working float EXR or
  Display PNG16 sequences at the first source's physical reference size;
  Grid preserves transparent stretch-to-fit cells and artist-selected raster.
  Real EXR headers, channels, alpha and source stat identities are validated
  for every decoded frame; signed sparse/Hold lineage, derived geometry and
  verified output pixels are journalled through the same durable queue.
  Unequal-PAR composites **cannot** export MP4 or include non-EXR members,
  offset/cropped EXR windows or source transforms. Legacy square-pixel
  EXR/PNG/TIFF composite export remains unchanged.
- **Verified mixed-media comparison export (opt-in v3)** additionally combines
  native full-window square-pixel EXR sequences with strictly bounded CFR,
  opaque YUV movie members, including movie-only comparisons. The Maya Export
  panel calls FFprobe to freeze each movie's single video stream, exact FPS,
  frame count/timestamps, matrix/range, pixel format and complete SHA256 byte
  identity. The artist must **explicitly confirm** the movie input-color/YUV
  interpretation before any export is enqueued; cancellation changes no
  review Source/session. Members keep separate logical IDs even when they
  reference the same physical media, with rational timing and signed sparse
  EXR Hold/Black/Checker/Error mapping. The CPU working-linear compositor
  outputs up to 120 frames of Working float EXR or Display PNG16 with verified
  per-member lineage and queue receipts. Actual FFmpeg result pixels and
  Source/Working/Display distinctions, cancellation, mutation/tamper, reversible
  overwrite and interrupted recovery are tested, including the Maya Export
  UI → durable queue → decoded EXR path. Inputs are limited to at most 240
  movie frames and 512 MiB per source; high-bit-depth movies in the *viewer*
  use separate bounded CPU SDR decoding, not a certified hardware HDR path.
  Mixed MP4 **output**, included audio, VFR, rotations, non-square movie PAR,
  ambiguous codec/color/alpha and complex transitions,
  tracking, speed changes, per-member grading and unverified GPU/OCIO delivery
  remain unsupported and fail closed rather than producing mislabeled output.
- **Managed EDL/OTIO record output (opt-in v4)** adds limited, verified
  sequence/record-timeline delivery to the same Maya Export UI and durable
  precision queue. Each of 2–8 hard-cut occurrences freezes its stable
  logical Source ID *and distinct occurrence ID*, exact record offset/duration,
  original source decode index/trim, rational FPS ratio, supported sparse
  image Hold behavior, color and alpha. The same physical source can appear
  multiple times with different cut names and trims without merging their
  review identity. Nonoverlapping hard cuts and record gaps use explicit
  source→record mapping; gaps export transparent RGBA zero, not the previous
  shot or fabricated HDR samples. The bounded CPU Working float EXR / Display
  PNG16 renderer validates exact movie provenance and EXR geometry, then the
  persistent queue verifies the frozen per-occurrence receipt before promotion
  or crash recovery. A real Maya UI → queue → FFmpeg → five EXR-frame test
  covers a repeated movie Source, two differently named cuts, a transparent
  gap and full provenance. Deliberately restricted to 120 record frames,
  square-pixel matching native raster, no more than 8 occurrences and exact
  source/session timebase conversion: overlapping edits/dissolves, independent
  editorial speed retimes, crossfades, transitions, movie output and cut audio
  are **not** yet supported. Imported EDL/OTIO still previews as before, but
  **managed output now validates the saved editorial contract against freshly
  probed media**: uncertain embedded source timecode, changed shot/relink
  origins, missing/source-out-of-range trims or imported unrepresentable
  transitions, motion, tracks and overlaps are rejected instead of silently
  outputting a different cut. Import warnings survive portable session
  save/reopen for this safety check. No source colors or timeline IDs are
  rewritten during validation.
- **Record-timeline live-picture/output parity:** For bounded, canonical
  square-pixel native EXR hard cuts with explicit Source alpha association and
  a proven exact source/record clock, the primary Maya Sequence viewer opts
  into the same CPU Source→Working float→Display transform as managed v4.
  This corrects a real mismatch: intentional picture gaps are transparent
  RGBA zero in the proven preview, matching independently decoded Working EXR
  and Display PNG16 instead of opaque Qt black. Real Source samples remain
  separately inspectable, with signed frame and distinct repeated occurrence
  identity. Saved editorial cuts may reuse one logical Source without replacing
  the duplicate occurrence's Source ID with a media path. Unsupported Qt-only,
  unclear-alpha, large, mixed-size, retimed or movie pictures retain legacy
  presentation without false native precision claims. The one durable queue
  and original SHA proof gates remain independent of read-only preview. Real
  Maya Session save/reopen now reconstructs the **full ordered edit occurrence
  timeline** (including repeated paths), rather than mistakenly restoring one
  unique Playlist source; a reopened signed record, transparent gap, unchanged
  Session dirty state, actual Export UI and independently decoded Working EXR
  were verified through the SAME durable queue. The current source-backed
  presented frame additionally requires a matching canonical original-file
  SHA256 before v4 record delivery: changed on-disk pixels, including same-size
  restored-mtime tampering, latch export rejection until explicit source
  recomposition and fresh presentation. This read-only proof covers the
  **currently presented frame**, not unviewed frames; the durable queue still
  independently freezes all output-range source hashes and output receipts.
- **File → Timeline Interchange export** now uses the same-file-system atomic
  publication for both EDL and serialized OTIO JSON. Existing edit lists are
  never truncated by a failed write; symlinked destinations are rejected.
  If CMX/OTIO cannot faithfully represent a transition, speed, overlapping
  edit or track, the actual Maya export workflow requests explicit approval
  **before** publishing the approximated edit list. Choosing No leaves any
  existing artist file unchanged. This is separate from managed media export,
  which still refuses these unsupported editorial effects altogether.
- **Mixed movie/EXR live precision:** the primary CPU comparison can retain
  high-precision scene-linear EXR floats alongside actual 16-bit RGBA samples
  decoded asynchronously from eligible 9–16-bit SDR movies. Source/member
  inspection and scopes identify this movie decode as **16-bit CPU SDR**,
  not native camera precision or a calibrated HDR monitor. PQ/HLG,
  8-bit/unsupported movie formats and unavailable decoders retain truthful
  display-only QImage fallback; cache generations and Qt shutdown are bounded.
- For managed Working/Display exports, EXR RGB(A) and PNG inputs can retain
  associated/straight-alpha semantics through float EXR and 16-bit PNG
  outputs. Select the source's alpha association in **Color Pipeline…**.
  Checker/Error placeholders are rendered as deliberate missing-frame slates,
  with signed frame IDs in the manifest, whereas a corrupt existing file
  fails the job. Alpha-bearing inputs are refused for MP4/JPEG outputs that
  would silently discard transparency. Unsupported image/codec variants are
  diagnosed rather than guessed.
- **Export → Export Queue** stores immutable source snapshots in an atomic
  journal (default `~/.framereviewer/exports/queue.json`, override with
  `FRAMEREVIEWER_EXPORT_QUEUE`). The queue uses one existing `ExportJob` at a
  time, private same-filesystem stages, FFprobe checks, cancellation, per-job
  logs, explicit retry and conservative interrupted-job recovery. Results are
  promoted only after verification. Pending jobs survive Maya restarts but
  never automatically rerun after a crash. Managed exports use this same
  serialized queue and private staging, not a second writer. Advanced mixed
  movie/composition transitions, unverified external or retimed managed audio
  delivery and unrestricted/VFR movie inputs remain unsupported.
- Explicit overwrites keep a private rollback copy of the previous movie or
  sequence until the promoted new output has passed verification and a durable
  queue success is recorded. An interrupted promotion can restore the original
  instead of treating a partial new output as a successful delivery. Recovered
  interrupted jobs still need an explicit Retry; no automatic re-publication.
- Current combined automated evidence (24 October 2026, after full native
  signed-offset EXR input canvas, v4 record preview→managed EXR/PNG16 parity,
  exact repeated Source edit identity, SHA256 comparison and user-only
  Recent history with isolated QA preferences): Maya 2024 `mayapy`
  offscreen **1,230 tests OK (3 skipped; 440.657 s)** using the
  explicit `QApplication` bootstrap; host Python **1,120 tests OK
  (545 dependency/Qt skips)**. Python 3.9 syntax validates **216 files**;
  compileall and `git diff --check` pass. These include real FFmpeg HDR EXR,
  native signed EXR window/canvas inspection, data-only precision scopes,
  physical-PAR comparison, signed sparse composite Working EXR/Display PNG16,
  real UI→durable queue→FFmpeg rotated/PAR Working EXR, rollback,
  selected embedded soundtracks, sample alignment, source replacement and
  cancellation during mux, local typed reply edit/delete, source-owned
  attachment Save As relocation and tamper/rollback, full-frame Sequencer
  integrity receipts, generation-safe Maya scene/reference restores,
  actual user-confirmed mixed movie/EXR Working delivery, movie SDR16
  inspection, duplicate-path source identity and rational-FPS mapping,
  real EDL/OTIO hard-cut/gap UI→queue→five decoded EXR frames, v4 queue
  tamper/cancellation/retry, independently proven external zero-origin PCM
  cut-to-record sample comparison and per-occurrence Maya Qt audio selection,
  signed stabilized EXR post-crash/rollback/tamper proof, verified editorial
  timecode refusal, protected
  atomic EDL/OTIO output and real FFmpeg/OpenCV signed sparse tracking,
  and Qt audio-clock stress. They do not
  certify live Maya 2023/2024 speaker, GPU HDR/OCIO display, cross-platform
  storage semantics or authenticated studio ShotGrid integrations.
- **Help → Capabilities / Support Report** shows runtime capabilities, source
  counts, playback diagnostics, and practical dependency remedies. Copying the
  report redacts sensitive credentials and media paths.

Production GPU/OCIO display parity, deep EXR, multichannel audio device routing,
real-server ShotGrid publication, editable stabilization and unrestricted
managed/composite exports still require dedicated implementation or interactive Maya validation. Tests
using fake SG endpoints or headless Qt are not certification of studio
infrastructure. See the **17-section reconciled status table** in
`OPENRV_IMPROVEMENTS.md`. This release does not represent full OpenRV parity.

## Requirements

| Dependency | Purpose |
| --- | --- |
| Autodesk Maya with PySide2 or PySide6 | Host UI, docking, Maya timeline synchronization |
| Python 3 compatible with your Maya release | Runtime |
| FFmpeg **and** FFprobe available on PATH | Probe, cache, timeline media decoding, and export |
| QtMultimedia | Audio playback and optional native video playback |
| NumPy and OpenCV (`cv2`) | **Optional:** tracking and perspective export |
| ShotGrid Python API / studio credentials | **Optional:** Flow Production Tracking browser |
| PyOpenColorIO | **Optional:** managed OCIO color transforms (explicit sRGB/linear fallback without it) |
| OpenTimelineIO | **Optional:** OTIO timeline interchange (CMX3600 EDL remains available) |

If OpenCV or NumPy cannot be imported in Maya, the tracking interface is disabled with an explanation; other review tools remain available.

FFmpeg binaries may be set explicitly:

```bash
export FRAMEREVIEWER_FFMPEG=/path/to/ffmpeg
export FRAMEREVIEWER_FFPROBE=/path/to/ffprobe
```

Playback cache defaults are 1280 pixels wide, 256 MiB, 96 frames maximum,
48-frame decode batches, and OpenRV's 25% look-back allocation. Facilities or
troubleshooting sessions can override them without changing source code:

```bash
export FRAMEREVIEWER_DECODE_WIDTH=1920
export FRAMEREVIEWER_CACHE_MB=512
export FRAMEREVIEWER_CACHE_FRAMES=144
export FRAMEREVIEWER_CACHE_BATCH=48
export FRAMEREVIEWER_CACHE_LOOKBACK=25
export FRAMEREVIEWER_CACHE_MODE=lookahead  # off, region, or lookahead
```

Cross-platform media can be relinked with path-mapping rules (source root to
destination root, `;` or newline separated). Rules are applied longest-match
first at path-segment boundaries, after environment-variable expansion:

```bash
export FRAMEREVIEWER_PATH_MAP="/mnt/show=/Volumes/show;//server/share=/mnt/share"
```

Values are validated and bounded at startup. Use **Help → Playback
Diagnostics…** to inspect the effective policy, active backend, resident cache,
planned window, pending uploads, buffering, and dropped/presented frame counts.
The **Playback → Cache** menu follows OpenRV's three public modes: Off,
Region Cache, and Look-Ahead Cache. Region Cache fills from the marked In point
until the range or memory/frame budget is exhausted; Look-Ahead Cache maintains
a direction-aware rolling window around the playhead.

For existing installations, `VIDEOCROPPER_FFMPEG` and `VIDEOCROPPER_FFPROBE` are accepted as compatibility fallbacks.

## Install in Maya

1. Place the **entire** `framereviewer` folder inside a directory on your `MAYA_SCRIPT_PATH` or directly in your user Maya scripts directory. Preserve its `__init__.py` and `icons/` folder.
2. Start Maya and run:

   ```python
   import framereviewer
   framereviewer.show()
   ```

3. For a shelf button, use the same two-line Python command. To load UI updates while developing:

   ```python
   import framereviewer
   framereviewer.reload()
   ```

   `framereviewer.reload()` already reloads the complete package. Calling
   `importlib.reload(framereviewer)` first is unnecessary, though it is
   supported. The package reload removes both `sys.modules` entries and cached
   package attributes so new UI code cannot retain old model classes.

The package's `restore()` and `shutdown()` entrypoints manage Maya workspace controls and background workers. Use these entrypoints rather than deleting the widget directly.

**Important:** The package import is lowercase `framereviewer`. Folder names on case-sensitive filesystems must match. Existing shelf scripts that import `videocropper` or refer to `VideoCropperWindow` must be updated to `framereviewer` and `FrameReviewerWindow`.

## Review and tracking workflow

1. Import media, open a saved notes JSON through **File → Open**, or choose **File → Playblast Current Shot** in Maya. Use the bottom timeline or arrow keys to navigate, and mark In/Out with `[` and `]`.
2. Open **Properties → Crop**, draw the crop in the viewer, and adjust it as needed.
3. Open **Properties → Track**. Choose position, similarity, affine, or perspective, then select **Track Forward**. Tracking requires image texture in the selected crop for reliable optical flow.
4. Play or scrub to inspect the tracked crop. You may adjust its position and size afterward.
5. Export the marked range and choose dimensions and destination in the export dialog.

Perspective mode needs multiple matched features; three points define an affine transform, while full homography requires four or more non-collinear correspondences. If features are not reliable, the crop holds its last good estimate instead of jumping to a false location.

## Playback troubleshooting

- **No audio:** check that QtMultimedia can load in Maya, the clip has a supported audio stream, and the volume is not muted. FFmpeg frame playback and audio playback use separate components.
- **Playback stalls or flashes an error:** confirm that FFmpeg/FFprobe are executable within Maya's environment. Run the binaries from a shell with the same studio environment to reveal dynamic-library conflicts.
- **Maya closes after repeated scrubbing/Play:** avoid unloading Python modules with active decoder threads. Use `framereviewer.shutdown()` before reloading the package. Report the Maya version, Qt binding, codec, and a minimal reproduction.
- **Tracking disabled:** install compatible OpenCV and NumPy builds in Maya's own Python environment, avoiding incompatible global Python packages.

## Icon assets

Icons are shipped as both SVG and 96×96 PNG in `icons/`. The GUI prefers PNG to avoid QtSvg plugin problems on studio machines; SVG sources are retained for editing. To rebuild PNGs after changing SVGs, install `cairosvg` and run `python tools/bake_icons.py` from the repository root.

## Development

```bash
python -m compileall -q framereviewer
```

### Tests

The Qt-independent suite runs under any Python 3 (from the directory that
contains the `framereviewer` package):

```bash
python -m unittest framereviewer.tests.test_sequence framereviewer.tests.test_notes ...
```

The window lifecycle, GPU-viewer fallback, and software compositor tests need a
Qt binding and NumPy, so run them (and ideally the whole suite) under Maya's
interpreter, which has both:

```bash
"/Applications/Autodesk/maya2024/Maya 2024.app/Contents/bin/mayapy" \
    -m unittest framereviewer.tests.test_lifecycle framereviewer.tests.test_gpu_viewer
```

`tests/test_lifecycle.py` constructs the real window and switches every
workspace tool, so a tool wired without its menu/shortcut/page entry fails the
test instead of crashing at launch in Maya. These tests force the standalone
(non-Maya-UI) code path, so they do not need a running Maya session. They SKIP
automatically when no Qt binding is importable.

The package deliberately avoids bundling FFmpeg, studio credentials, and vendor libraries. FFmpeg decode workers own subprocess handles; decoded Qt images are transferred to the main thread before conversion to `QPixmap`. Keep that lifetime/thread distinction when changing playback.

### Package layout

| Module | Responsibility |
| --- | --- |
| `app/` | Maya window composition, persisted settings, scene state, and About UI |
| `core/` | Qt/Maya compatibility, undo, and dependency-light utilities |
| `playback/` | Player, frame sources, rolling cache, scheduling policies, thumbnails, waveform, and multi-clip composition |
| `playback/composition.py` | Qt-free multi-source composition model (sequence/grid/difference modes, concatenated timeline, grid geometry) |
| `media/` | FFmpeg probing/process helpers, EXR loading, playlists, and sequence/time model |
| `media/sequence.py` | OpenRV-faithful sequence discovery, frame expressions, and the display/source/FPS/timecode mapping |
| `media/geometry.py` | Qt-free image windows (aperture/data/display) and normalized crop geometry |
| `app/session.py` | Portable, Qt-free source/session model (ids, representations, timing, review state) |
| `app/session_controller.py` | Live source identity, review-state projection, relinking, atomic session save and recovery |
| `media/resolver.py` | Unified media/sequence/folder/notes/session import classification |
| `media/interchange.py` | CMX3600 EDL and optional OTIO exchange through Session/Composition |
| `media/time_alignment.py` | Source-timecode to local-decoder trim mapping with explicit ambiguity warnings |
| `core/frame_payload.py`, `viewer/sampling.py` | Owned frame buffers, stage-aware pixel sampling and histogram |
| `viewer/color.py`, `exporting/spec.py` | Explicit CPU color-pipeline state and immutable output manifests/verification |
| `core/support_report.py` | Redacted capability and playback diagnostics |
| `viewer/scopes.py` | Cancellable histogram, waveform and vectorscope computation/display |
| `core/path_mapping.py` | Studio/OS path remapping for cross-platform media relinking |
| `core/capabilities.py` | Startup dependency probe with status, detail, and remedies |
| `viewer/` | Playback surface, note/crop overlays, timeline, display color, tracking, GPU multi-viewer, and software compositor |
| `viewer/gpu_viewer.py`, `viewer/compositor.py` | GPU (QOpenGLWidget) multi-source viewer and its NumPy software-fallback compositor |
| `notes/model.py`, `notes/serialization.py` | Qt-independent note data, validation, compatibility, and versioned JSON |
| `notes/propagation.py`, `notes/merge.py` | Note copy/paste/propagation and ID-based merge/conflict handling |
| `notes/tool_registry.py` | Expandable annotation-tool metadata registry |
| `tools/` | Top-level Media/Crop/Draw/Track/Review/Export integration registry |
| `widgets/` | Reusable Qt widgets kept out of the application window |
| `exporting/` | Export options, FFmpeg export jobs, and the Qt-free burn-in template/token model |
| `integrations/shotgrid/` | Optional Flow Production Tracking client and widgets |

Annotation tools are declared once in `notes/tool_registry.py`. Their label,
icon, tooltip, default size, settings presentation, and size limits drive the
toolbar and preferences. Rendering and pointer behavior remain in the viewer
for now; new behavior should be added behind the registry rather than by adding
another parallel collection to `app/window.py`.

## Status

Active Maya pipeline tool. Playback and tracking changes should be verified within the specific Maya build and with representative studio footage before broader deployment.
