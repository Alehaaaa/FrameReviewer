# FrameReviewer improvement plan aligned with OpenRV

## Purpose and boundary

This is the implementation roadmap for adopting the most useful OpenRV review
concepts without turning FrameReviewer into an OpenRV clone. Integration with
the current application is the first priority: new work must extend the existing
source, session, playback, composition, note, crop, export, and Maya-state logic
rather than create parallel systems.

Reference: `/Users/aleha/Downloads/OpenRV-main`  
FrameReviewer state reviewed: October 2026

FrameReviewer remains a focused Maya-native review tool. OpenRV's Mu runtime,
node editor, package manager, device framework, and network protocol are not
targets. Priorities are **P0** correctness/foundation, **P1** daily review,
**P2** production depth, and **P3** studio-specific work.

## Reconciled implementation audit — updated 24 October 2026

This is a live source-and-test audit of all 17 sections, **not** a claim of
production certification. "Integrated" means the actual Maya window and
viewer are wired to that subsystem; "partial" means the listed acceptance
criteria are not all satisfied. Some tests are headless Maya `mayapy` rather
than an interactive hardware/studio session. No pending feature was rebuilt
merely because it appears in the original specification below.

| Section | Implementation actually present | Remaining acceptance gap |
| --- | --- | --- |
| 1. Live session | **Integrated** `SessionController`, stable IDs, per-source review, portable atomic sessions and cross-directory Save As of verified locally managed attachments (staged exclusive copy, SHA256/size, rollback, existing asset safety), dirty-only recovery with bounded three-snapshot history; unchanged review captures verified not to schedule recovery. A saved signed editorial record with two cuts of ONE logical Source now reopens as two original ordered occurrences with exactly preserved trim, transparent gap, clean Session dirty state, and independently queued Working EXR delivery | Full simulated Maya crash/reopen, attachment relocation on network storage and production workspace reload stress |
| 2. Source resolver | **Substantially integrated** unified import, network path mapping, sequence origins, offline relink, roles; signed Skip/Hold/Black/Checker/Error from probe through Player, comparison, viewer, saved Session and managed export. Authored `999-1009x2#` ranges keep missing leading/trailing frames after reopen and exclude outside matches; broken present files show distinct decode failure slates. Black/Checker/Error now use one deterministic opaque RGBA8 raster for actual Qt viewer and native-size managed Display PNG16 (all channels independently decoded); no fabricated native HDR sample. Supported native EXR part/layer selection is explicit, source-owned and preserved through relink/session recovery. Opt-in SHA256 before/after selected-native decode preserves canonical original-file/signed-frame provenance and invalidates stale native+Qt cache on same-size/restored-mtime tamper; synthetic missing placeholders never gain proof. The File → Open Recent menu persists only explicitly user-opened media/session/notes, distinguishes historical expired QA fixtures from even a genuine artist-opened temporary file, and retains offline removable/network media without a misleading modal. The full QA suite isolates its disposable preferences and never migrates real legacy user settings. Verified bounded native CPU signed-window EXR review now works without Python OpenEXR for single-part RGB(A) NONE/ZIPS/ZIP scanlines, retaining native HALF/FLOAT/UINT, source signed coordinates and true padding; malformed chunks fail closed | Folder-watch, native deep EXR and advanced multipart workstation decoding, extended media relink across studio storage changes |
| 3. Geometry | **Substantially integrated primary Qt view and bounded output** unified invertible source/display/viewport affine mapping, PAR, 90° rotations, flip, zoom, notes, onion, crop, selection and A/B probes; signed native EXR dataWindow pixels placed on verified displayWindow canvas, also on independently proven nonzero-origin scanline EXR without Python OpenEXR. Live CPU comparison rectifies unequal member PAR in the first member's physical field while Grid retains stretch-to-fit. Supported single-source managed Working EXR/Display PNG output freezes and bakes PAR, integer crop, flips and quarter-turns into square-pixel delivery. Opt-in all-native-EXR composite image sequences also freeze and verify unequal member rational PAR, output physical field, signed sparse identity and alpha. Precision CPU comparison now rectifies each original member at pixel centers with bounded native PAR and true transparent coverage; Grid samples only into its final cell; direct Source switch clears outdated composite/export intent. A/B/Flicker now also rectify equal-PAR but DIFFERENT native width/height into the first Source physical field (transparent outside, physical center crop for larger plate) while identical native sizes preserve their original path; GRID stays square/stretch; warm mode switches restore physical geometry | Actual native GL parity, arbitrary/offset EXR windows in managed writer, mixed-media PAR composites and advanced stabilization/geometry |
| 4. Color precision | **Substantially integrated precision CPU pathway** native OpenEXR HALF/FLOAT/UINT when installed, tested real FFmpeg canonical FLOAT32 RGB(A) fallback without OpenEXR Python plus exact native CPU HALF/FLOAT/UINT signed-window scanline reader for NONE/ZIPS/ZIP; signed HDR FramePayload into main float-derived Qt SDR display, original sample inspection, per-source OCIO/linear color/alpha state, float working-light comparison and managed float/16-bit outputs. Supported 9–16-bit SDR movie members now have bounded async FFmpeg RGBA64 CPU samples labeled SDR, composited with original HDR EXR floats | Production OCIO config/view/look monitor parity, native GPU shader parity, verified PQ/HLG/HDR movie handling, deep/tiled/multipart/PIZ EXR and named-layer precision without native OpenEXR, optimized async huge EXR decode |
| 5. GPU primary | **CPU-primary plus experimental guarded GPU helper** main Qt reviewer displays signed float-derived color-correct SDR; opt-in GPUMultiViewer guards existing-current-GL context, float RGBA32F uploads, real shader modes/cleanup and honest fallback status, using the same CPU Source→Working→Display transform before upload; never represents true hardware HDR/OCIO shader parity | Actual QOpenGLWidget in production primary annotations/overlay, real workstation GL framebuffer/parity/driver/context-loss and genuine HDR monitor/studio OCIO GPU validation |
| 6. Inspection/scopes | **Integrated precision-staged CPU inspection** original FramePayload Source/Working HDR and negative scopes, actual presented SDR Display scopes, per-member A/B inspection, pinned native values and stale-generation fences. Signed EXR absolute pixels/padding are identified explicitly; Source/Working can scope either EXR dataWindow coverage or the padded display canvas. Live member probes share the physical-PAR comparison coordinate model. The primary Maya View menu now has a read-only nearest-neighbor 9×9 pixel magnifier with real UINT/HALF/FLOAT source storage, stage/provenance and generation-safe PAR/rotation/flip/A-B pins, plus an unobtrusive mouse-transparent viewport panel. Primary pinned/hover comparison inspection now identifies the actual visible operand(s) of Wipe/Checker/Split/Grid/A-B/Flicker and Blend/Over/Difference at the current cached frame, with native Source/EXR padding/UINT storage and a presented-frame cache-key fence | Workstation GPU color stage and calibrated hardware monitor proof |
| 7. Comparison | **Substantially integrated, verified v4 PCM plus bounded SDR MOV and RGBA16 NUT** one viewer record clock, A/B/flicker/blend/wipe/split/checker/over/difference/grid, signed sparse Source identity, exact rational frame maps, per-member precision/color/alpha. v4 2–8 hard-cut EDL/OTIO occurrences produce Working EXR or Display PNG16 with independently selected embedded/external 48k PCM in one durable folder. A strictly bounded 1..3 ordered set of independent canonical external PCM events can overlap per-cut sound and each other at frozen source/record offsets and exact gain, including picture gaps, using the same actual PCM for Qt, waveform and WAV. Optional Display SDR MOV RGB8/PCM and NUT FFV1 RGBA16/PCM share that verified PNG16/WAV folder; NUT carries exact 1/48000 stream time bases, 2002-tick NTSC video PTS and all decoded straight-alpha pixels are rechecked. Live comparison native-pixel owners now follow visible masks; direct single-Source switching releases stale composition. Unequal-PAR A/B/Flicker now display the selected Source on a bounded first-Source physical reference raster in native Working float; original Source inspection maps back into the selected native frame. The Export UI permits v2 physical delivery only when the exact full-reference Working float frame is genuinely presented and its source/frame/QPixmap receipt still matches, otherwise fails closed. Actual native original SHA+requested/held signed frame are separately revalidated; changed media latches managed selected delivery into rejection until explicit composition reload and new presentation. Supported bounded EXR hard-cut record preview now matches queued Working EXR and Display PNG16 Source→Working→Display, including signed repeat occurrence IDs, explicit alpha and truly transparent record gaps; unsupported native or Qt-only cases are visibly distinct from proven HDR. Record v4 now demands the genuinely PRESENTED source-backed Working float frame and its unchanged cached original SHA256 before managed queue preflight; same-size/restored-mtime tamper sticky-blocks the composition until explicit artist recomposition and presentation. Only the current presented frame receives this proof, not unviewed future frames. | Calibrated physical sync, unbounded multitrack count/crossfades/effects, scene-linear HDR movie, non-NUT compatible exact 16-bit movie, general retime/VFR/overlapping picture and native GPU/OCIO |
| 8. Transport/cache | **Substantially integrated** real-time vs all frames, signed JKL, loop/ping-pong, exact rational mixed-member clock, bounded cache/generation cancellation, and optional <=2-frame asynchronous movie SDR16 sidecar for high-bit-depth CPU inspection. Generation/source/frame/request-serial gates now fence reentrant decoded pixel/payload publication and old FramePump signals, while composite loading reissues initial frameChanged. Signed Hold origins use O(log n) lookup; opt-in SHA256 native source proof is invalidated on edited/evicted files, with no hashing overhead in default sequence playback. A private SequenceSource revision gate now rejects cached native float/QPixmap/SHA publication when source close, color state, hole policy or subscriber callbacks reenter during read/hash/render, and suppresses late frameAvailable after shutdown. 100081-frame sparse cache policy switches visit resident placeholders only; oversized synthetic cards clamp to existing Qt/cache budgets without invented source precision | Quantified studio perf gates, external audio clock device stress, proxy tiers, exhaustive cache lane states and repeated real-Maya scrub stress |
| 9. Audio | **Verified per-occurrence record PCM plus up to THREE explicitly ordered independently authored overlapping audio-only WAV events** selected embedded or proven canonical external WAV, exact `24000/1001` / 2002-sample-frame cuts, trim/gap/repeated-Source identity; separately placed zero-origin audio events each with source in-frame, record offset, bounded length and exact mute/half/unity signed PCM16 sequential saturating mix (authoring order deliberately affects clipping). Actual GUI waveform displays mixed/overlay origins and nonzero authored gap, and fake-device Qt QBuffer seeks/plays the exact mixed bytes without creating a second clock or autosave. Same mixed samples validated in WAV/MOV/NUT via one queue | Physical speaker onset/latency and hardware drift unmeasured; more than three extra tracks, live effects/keyframed gain, sample-unaligned offsets, compressed audio codecs, independent retimes, EDL/OTIO audio-only roundtrip, surround/DSP/crossfades unsupported |
| 10. Typed notes | **Integrated core workflow** `ReviewDocument` in Draw/Review/session/JSON, stable shape IDs, drag/undo/onion/layers and dirty-safe merging; local unsent reply edit/delete, stable IDs and optimistic safeguards. Saved-session-only file/still Browse/Add and local unsent reference removal are wired to portable source-scoped bounded copies, no-symlink checks and preserved drafts; cross-directory Save As preserves verified assets atomically without deleting originals or old files; no remote writes | Orphan-copy garbage collection policy, manual external file moves, remote attachment/reply operations (separate consent/permissions), advanced conflict resolution and markup tools |
| 11. Tracking | **Substantially integrated with bounded native precision stabilized DELIVERY** four OpenCV motion models, signed sparse original source labels, source-owned tracking_v1 with real LK quality/loss, per-key manual translation/undo. Opt-in Stabilize View pans *all* Qt image/annotation/crop/pin scene items together. New Maya Export option freezes identical signed original Source/anchor/track/rational FPS and hash-proven EXR inputs, renders per-frame native linear Working RGBA float32 with exact native bilinear translation, real zero-alpha unsupported edges and independently decoded float pixel verification via the SAME durable queue | Reverse tracking, perspective/affine manual editing and stabilization, long-form/offset EXR geometry, crop/display/OCIO/GPU stabilized export, Maya2023 interactive performance and calibrated monitor checks |
| 12. Maya context | **Partial but integrated** current-shot playblast, Sequencer playlist, deferred Maya sync and reentrant scrub/late-shutdown guards, generation-fenced Maya scene restoration and source identity. Sequencer rechecks unsaved/untitled state and saved-file revision before cache lookup; dirty/unknown or explicitly refreshed scenes get isolated non-destructive revisions. Whole-frame-set manifest size/SHA256 and filename verification now rejects corrupted/missing/altered playblast frames rather than merely trusting the first frame | Content-specific unsaved scene diff provenance, centralized Maya context display, full camera/object navigation, large-scale playblast performance and interactive real-Maya GUI/reload stress |
| 13. ShotGrid | **Substantially integrated local workflow** authenticated version browser, source/Version identity, typed-note/status durable outbox with separate consent, idempotent marker search; user-mediated read-only remote/local conflict inspection, explicit ack/requeue/status-rebase/discard with live Version/Note recheck and stale asynchronous generation fences | Actual studio permission/auth/server tests, attachment authoring/publication, scheduled assisted retry and distributed transactional compare-and-set (SG API lacks atomic CAS) |
| 14. Export | **One durable v4 EXR/PNG16/WAV queue plus independently verified SDR MOV and lossless Display RGBA16 NUT** private staging, immutable source SHA, independently decoded image/PCM/PTS source proofs, cancellation, receipts, rollback, reversible overwrite, interrupted recovery/explicit retry. `record_av.mov` remains H264 RGB8 black-matted Display SDR. Optional `record_rgba16.nut` FFV1 `gbrap16le` preserves exact straight RGBA16 Display SDR and 48k PCM, with exact 24000/1001 frame PTS in NUT; both may coexist inside one recoverable PNG16/WAV folder and validate separate receipts. Up to three explicitly authored ordered overlapping external WAV events mix into the same exact PCM, with independent original SHA/stream receipts. Stabilized native Working float EXR remains independently pixel-verified. Single-source native-size Display PNG16 intentional Black/Checker/Error raster now byte-matches Qt viewer with signed hole identity and existing durable receipts. Real Maya2024 primary non-square-PAR EXR→blend/native inspection→actual Export UI→one staged Working float EXR and Display SDR PNG16 was independently decoded for signed HDR RGB, fractions of alpha, original frame labels and correct square-pixel output size; unequal-PAR A/B selected-B real Window→Export UI→ONE durable Working float EXR independently matches primary presented physical pixels, native signed Source IDs and fractional alpha. Unpresented/Qt-only/downsampled A/B/Flicker still fail closed before enqueue. Real equal-PAR unequal-size Maya B8×8 vs A16×8 Working float EXR AND SDR PNG16 were independently decoded and checked for centered transparency, signed negative RGB and .25 linear→.537 sRGB alpha-preserving quantization; post-preview original SHA corruption blocks queue until explicit reload. Bounded native EXR record-preview Working float and Display PNG16, true gap zero alpha and exact repeated Source occurrence IDs were checked through the real Maya Export UI and SAME durable queue; offset-window EXR remains refused by managed export until an independent matching writer is established. Reopened repeated Source record edits additionally reach the actual same UI/queue/Working float renderer only after the artist's current source-backed preview has an unchanged file SHA and matching Source/frame/QPixmap receipt, with explicit recompose after altered media | No standalone mux, native HDR movie or production HDR display/OCIO, arbitrary retime/VFR/fades/effects/deep/offset EXR, universal NUT player support, hardware certification |
| 15. Timeline/interchange | **Absolute source TC, rational repeated-occurrence mapping and explicit per-edit/overlay audio-loss warnings** hard cuts/gaps keep native source↔decoder identity and exact NTSC clock. Session can explicitly remember v1 ONE or v2 1..3 ordered audio-only events (independent source in, record offsets, bounded trims/gains) only when current edit fingerprint matches, without pretending it is source media proof. EDL/OTIO roundtrip warns and requires informed VIDEO-only consent for either independent per-cut sound or overlapping audio-only events; stale fingerprints fail closed. Verified v4 media deliverables preserve mixed 48k PCM in WAV/MOV/NUT without silently making interchange richer than it is; repeated imported record occurrences of one path keep the saved distinct edit identity and the actual logical Source ID on every new Maya composition AND after Session save/reopen, rather than collapsing to one unique Playlist row; unsupported stale/absent occurrences refuse remapping | Real installed OTIO adapters/multitrack complex schema, arbitrary source/sample delays, complex audio-only interchange, transitions/retimes/picture overlaps, calibrated acoustic sync, native HDR film mastering |
| 16. Architecture | **Partial** modular packages, native-style workflow menus/sidebars, explicit artist-only Open Recent registration with durable explicit user provenance, old expired QA fixture cleanup, disabled-but-preserved offline opens, and isolated disposable Maya test QSettings, capability report, narrow services, generation-aware workers | `window.py` remains monolithic; extract mature controllers and add complete structured diagnostics/plugin boundaries |
| 17. Validation | **Strong Maya 2024 offscreen GUI and real-FFmpeg foundation** native signed HDR EXR, CFR source/multistream/external stereo PCM, exact `24000/1001`, verified signed and ordered 1..3-overlay mixing, nonzero intentional picture gap, Qt fake-device sample-address proof, actual Maya Export UI→same durable PNG16/WAV/MOV+NUT independently decoded pixels/alpha/PCM and 1/48000 NUT stream PTS; hostile source/hash/media/receipt, rollback/recovery and stabilized float EXR coverage. Real Maya native-inspection/geometry/padding tests and primary sparse viewer → Export UI → one queue → independently decoded PNG16 checker per-pixel signed identity/alpha proof also pass. Real Maya2024 GUI native unequal-PAR EXR compare/seek/Wipe/Checker/Grid inspector lineage and Working EXR/Display PNG16 one-queue delivery were independently decoded and checked against native signed HDR/fractional alpha/original-frame oracle; direct source switch and stale-frame receipt fences were exercised. Actual Maya same-PAR unequal-width A/B Session save/reopen, source identity, original fractional alpha and one-queue Working/Display field delivery now pass; FFmpeg source byte mutation with restored size/mtime is rejected by decoded original SHA (with explicit artist recompose required), and Qt reentrant source/PCM seek tests confirm new generation/anchor safety. Bounded original signed offset EXR HALF/FLOAT/UINT canvas was proved in actual Maya without Python OpenEXR, and real per-record Working EXR/Display PNG16 image parity was compared after independent one-queue decoding. Tests also prove that internal loads and Maya QA cannot pollute real artist Recents. New native SequenceSource reentrant close/precision/cache/QPixmap/SHA tests (11 actual Maya) and 100081 signed sparse policy budgets, as well as saved record Session reopening→real managed queue→independent EXR decoding and original SHA same-size tamper refusal, are checked. Latest full-suite totals are reported separately after a completed regression pass, not inferred from focused tests | Physical speaker latency, interactive Maya2023, hardware HDR GPU/OCIO, authenticated ShotGrid, Windows/Linux/network storage, large studio load NOT certified |

**Highest-priority unresolved release gates:** interactive Maya 2023/2024
reload/scrubbing/audio-device stress; native GPU framebuffer/OCIO monitor parity
on representative production footage; real enterprise Flow authentication and
write permissions; unrestricted managed media/audio and composition staging.
Do not mark these complete based solely on `mayapy`.

### 24 October 2026 — record Session recovery, original picture SHA and cache resilience

- A real Session reopen exposed an editorial regression: an imported edit
  containing two separately trimmed occurrences of one logical Source had one
  Playlist row and silently reopened as a plain SequenceSource. The actual
  Window now restores the saved ordered source/occurrence list rather than
  inferring the record clock from unique media paths. Missing or ambiguous
  original members fail closed instead of silently appearing as a different
  edit. Real Maya2024 Session save/reopen kept the original Source ID, repeated
  occurrence IDs, original signed EXR frame -1, opaque-versus-transparent gap
  alpha contract, clean dirty state and user Recents; a reopened actual Export
  UI delivered five independently decoded float Working EXR frames through
  the SAME existing recoverable queue.
- Opt-in verified v4 EXR preview now SHA-proves its current real source-backed
  presented picture (including genuine sparse Hold origin) before allowing
  managed record preflight. Changing original bytes while preserving on-disk
  size/mtime blocks that edit, and returning the old bytes does NOT silently
  restore trust until explicit artist recomposition and newly presented media.
  The old stronger A/B/Flicker source-integrity model and independent queue
  source/output hashes remain unchanged; no assertion is made that all future
  frames in a queued range were viewed.
- Fixed FOUR reproduced SequenceSource races around synchronous shutdown
  during precision decode, reentrant color/placeholder changes and cache
  listener callbacks: a private revision token now prevents obsolete QPixmap,
  native float pixels or SHA cache from reappearing after lifecycle mutation.
  Late signals are suppressed; bounded large sparse policy switches and
  oversized Qt-only slates stay memory-safe without inventing HDR originals.
  Real Maya/FFmpeg new 11 tests and combined 85 regression tests PASS; prime
  focused real UI/source/record/PCM/SHAs/EXR suite passed 84 tests.
- FrameReviewer native Open Recent now records an explicit per-user action
  marker. Vanished old QA temp fixtures are removed at menu refresh only if
  not genuinely artist-opened; missing user network/removable paths remain in
  order, disabled until reconnected. This fixes the reported expired
  fr-physical-comparison-ui/EXR history without wiping legitimate review
  media or showing spurious modal warnings. Direct focused Maya test settings
  as well as full suite preferences are isolated from real artist preferences.
- Studio release blockers remain unchanged: hardware HDR GPU+OCIO calibration,
  Maya2023 interactive behavior, production ShotGrid permissions, unrestricted
  OTIO/EDL transitions/audio-only tracks and cross-platform network storage.

### 24 October 2026 — native signed EXR, record-picture parity and clean Recents

- Real Maya2024 without Python OpenEXR can now review independent, signed
  nonzero-origin scanline OpenEXR in the native primary viewport: a bounded
  CPU reader proves absolute chunk Y, native channel bytes, HALF/FLOAT/UINT
  dtype, original dataWindow and displayWindow, scene-linear signed RGB,
  fractional alpha and unfilled transparent canvas padding. Maya media probe
  checks *decoded* pixels and header geometry before approving display canvas.
  The original canonical FFmpeg path remains unchanged and is not used to
  infer signed placement. Unsupported compression, deep, multipart, arbitrary
  layers, invalid Y or changing per-frame geometry fail closed; managed
  offset-window EXR export is deliberately still unavailable.
- Record-timeline native EXR preview now supports an opt-in, bounded v4
  physical-picture path for original Source→Working float→Display parity.
  Record gaps are transparent RGBA zero, not opaque UI black. Actual signed
  Source frames and stable unique per-edit occurrence IDs are preserved across
  repeated paths; original native pixels remain separately inspectable and
  placeholder slates never acquire invented HDR. This is only activated for
  proven unretimed nonoverlapping square-PAR, equal-raster EXR images with
  explicit authored source alpha. Unsupported movies, large plates or
  unverified color/geometry retain the legacy unclaimed visual preview.
  Real Maya Window→Export UI→ONE durable queue→independent Working EXR and
  Display PNG16 decode confirmed picture parity, including gaps and alpha.
- Corrected production Open Recent to record explicit user File/Import/drop
  actions, not every automatic Source/composition/restore/test load. The Maya
  test suite now uses a separate temporary IniFormat preferences store and
  never reads the artist's actual settings or legacy VideoCropper preferences.
  Identified stale QA test paths were selectively removed from the real native
  QSettings list without deleting genuine artist media history. Menu refresh
  synchronizes cross-process preference changes. Dirty-only Session autosave,
  three-snapshot retention, PCM and the one durable queue are unchanged.
- Hardware/studio GPU HDR/OCIO, audio device, Maya2023 interactive, remote
  ShotGrid and network/Windows/Linux production release claims stay open.
  Full regression results below are measurement-based, not a claim that all
  17 sections have reached production acceptance.

### 24 October 2026 — audio routing and correctable source-frame tracking

- Kept the existing single `Player` and transport clock, correcting a silent
  signed-delay limitation: native Qt video/audio cannot offset its own embedded
  track, so routed sound explicitly uses existing FFmpeg video plus Qt audio
  for positive/negative delays. Live route changes preserve player position,
  marked trims and loop/play state; pending seeks, audio EOF and loop wrap are
  handled without falsely claiming device sample accuracy. Native Qt6 track
  ordinal zero is actively restored after a non-default choice. Read-only
  **Playback → Audio → Routing & Sync Diagnostics…** displays actual backend,
  selected stream, clock validity, drift and unsupported Qt5 DSP capabilities
  without modifying Session or autosave state.
- Tracking now returns per-native-source-frame real LK feature count, RANSAC
  inliers, forward/back consistency, reprojection error, lost state and
  heuristic confidence (not a calibrated probability). Lost motion holds its
  last real transform without inventing center/flow. FFmpeg B-frame CFR tracking
  seeks are tested against source pixels across multiple GOPs; prior input-only
  seeks could begin on the wrong decoder index.
- The actual Crop/Track UI now stores bounded original signed frame labels and
  quads/centers/quality under the stable logical Source with rational FPS,
  flip and native raster validation. A relink missing even one recorded source
  key refuses the saved track instead of using nearest-frame mapping. Changed
  crop offsets persist only on real edits. **Correct Current Frame** bakes one
  manual translation key, marks it as manual (never measured confidence), and
  supports Undo/Redo. Export refuses known lost motion inside the selected
  range before showing a delivery dialog or touching the queue. Advanced
  interpolation, arbitrary manual affine/perspective correction and an
  independent stabilization consumer were pending at that checkpoint;
  see the newer managed Working EXR acceptance below.
- Tested with real FFmpeg audio/CFR B-frame footage, OpenCV and Maya2024
  offscreen UI; these tests do not prove exact Maya2023 studio GUI, hardware
  HDR/OCIO parity, physical sound-card delay, production ShotGrid or network
  share behavior. All 17 sections remain open until their acceptance gates.

### 24 October 2026 — subsequent production integration

- Main PlaybackSurface now supports two pinned source-image pixel locations,
  source/working float RGB or SDR display delta based on actual comparable
  color stages, live updates on presented media and image-normalized anchors
  that survive zoom/flip. Decoder-error slates clear stale precision readings;
  the native Image Inspection menu and status bar are wired without affecting
  source review dirty state or three-snapshot recovery retention.
- Audio transport now fences soundtrack EOF, stalled playback clocks, late
  audio recovery and in-flight Qt seek acknowledgments. Real-time video can
  use the original video clock during a stalled audio backend rather than
  hanging/restarting repeatedly; tested against fake Qt media objects and
  rapid seek/play/reload cases. No physical speaker device was exercised.
- Managed movie export now accepts explicitly opted-in bounded **constant
  frame-rate** sources. Frozen user confirmation of input color, alpha (when
  present) and YUV matrix/range prevents silently guessed studio transforms;
  audio omission is explicitly confirmed. The FFmpeg float decode pipeline
  generates stage-distinct Source, Working float EXR, Display 16-bit PNG/MP4
  outputs; full frame-map decode indices, original signed labels, expected
  frame count, rational FPS, alpha/output format and source-color provenance
  participate in reversible private queue promotion and crash recovery.
  Real FFV1, tagged H.264/YUV and alpha QuickTime inputs verify pixels,
  color differences, frame maps, cancellation, rollback and journal state.
  The Source menu continues to select the legacy unmanaged path; precision
  movie Source is supported by explicitly frozen job API. VFR, unrestricted
  input color profiles, movie audio/clip crossfades, composites, tracking and
  GPU/OCIO studio monitor parity remain open.
- **Prior integration baseline:** Maya 2024 `mayapy` offscreen **642 tests OK
  (1 skipped)**; system Python **639 tests OK (203 dependency/Qt skips)**.
  Superseded by the later verified full integration result below.

### 24 October 2026 — managed comparison, geometry and HDR scopes integration

- Primary view applies a shared invertible source/display/viewport geometry:
  source pixel aspect ratio and 90-degree rotations transform the Qt scene,
  image, crop, annotation selection, onion ghosts and A/B sample pins together.
  Rotation follows source review/session state; redundant updates do not arm
  automatic recovery. Unequal member PAR, rotated or non-square source export
  still require explicit export geometry support and are rejected or clearly
  diagnosed rather than silently written with incorrect framing.
- Scopes in the actual review window offer **Source**, **Working** and
  **Display** stages. Original EXR/HDR `FramePayload` floats (including
  negative and over-range values) reach the source histogram; color-managed
  float transforms feed working scopes; display scopes sample the actual SDR
  pixmap. Source precision is not fabricated from working composites, and
  missing/failed decode samples cancel stale scope requests. Actual FFmpeg
  EXR tests drive the complete window and scopes UI.
- For supported image-sequence comparisons, the existing Export panel now
  snapshots every logical member's color interpretation, signed sparse frame
  map, Hold/Black/Checker/Error policy, alpha, rational FPS, source identity
  and selected mode. The same CPU floating working-space compositor used for
  live comparisons drives supported A/B, Blend, Wipe, Split, Checker, Over,
  Difference, Inverted Difference and Grid managed **Working float EXR** and
  **Display PNG16 / verified opaque MP4**. User confirmation explicitly
  acknowledges omitted audio. The existing serialized export queue checks
  exact frozen member maps and color evidence through staged verification,
  reversible overwrite and crash recovery; corrupt media or unsupported
  geometry fails without deleting a previously verified output. Real FFmpeg
  alpha/HDR/negative/sparse/composite/mode, QThread cancellation and journal
  recovery regressions exercise this workflow.
- **Latest full verified suites (post-native signed EXR primary canvas, v4
  record-picture parity, repeated Source ID repair and test-isolated real Open
  Recent):** Maya2024 `mayapy` offscreen **1,230 tests OK (3 skipped;
  440.657 s)** using explicit `QApplication` test bootstrap; host Python
  **1,120 tests OK (545 dependency/Qt skips)**; **216 files** parse under
  Python 3.9 grammar; compileall and `git diff
  --check` pass. Offscreen Maya QWidget GUI→durable queue→decoded deliverables
  was tested; no interactive workstation GUI session was available. Actual GL HDR framebuffer,
  OCIO shader/display parity, audio device timing, studio ShotGrid permissions,
  Maya 2023 GUI and Windows/Linux/network filesystem behavior remain
  outstanding release gates. Supported v3 movie-member and v4 nonoverlapping
  hard-cut/gap EDL/OTIO Working/Display output remains bounded to EXR/PNG16;
  v4's separately verified Display SDR 8-bit MOV and FFV1 RGBA16 NUT options
  may accompany proven PNG16/PCM in the SAME private queue and are not HDR.
  Arbitrary MP4 composite delivery, unverified audio, transitions/retimes,
  non-square record PAR, unsupported rotations/crops, VFR and unproven
  OCIO/GPU operations continue to reject explicitly.

### 24 October 2026 — native EXR and embedded audio delivery follow-through

- Legacy EXR reader adapters no longer receive unexpected optional selection
  arguments. Native part/layer enumeration now handles multipart and header-only
  APIs, with explicit artist selection retained per logical Source through
  relink/session recovery; unchanged selection does not create an autosave.
  The FFmpeg float fallback rejects nonzero/cropped or mismatched EXR windows,
  UINT32-to-float precision loss, unsupported AOV/channel interpretation and
  unsafe raster geometry rather than falsely asserting pixel correspondence.
  Non-EXR probes remain Qt-free on standalone host Python.
- The actual Playback → Audio menu exposes bounded per-source embedded stream
  selection, showing both Qt ordinal and FFprobe stream identity. The chosen
  track is the one frozen and verified for eligible opt-in managed CFR Display
  MP4 export; no-selection and repeat selection leave the review unchanged.
  FFprobe timing preflight rejects missing PTS and multi-video inputs. A staged
  movie cannot be silently replaced between float video decode and AAC mux:
  device/inode/size/timestamps are checked across delivery and a changed
  source is rejected without promoting output. FFmpeg real audio fixtures
  correlate selected sample trims at `30000/1001` and verify mid-mux cancel.
- **Combined after-integration verification:** Maya 2024 `mayapy` offscreen
  **703 tests OK (2 dependency skips)**; standalone host Python **700 tests OK
  (255 optional-dependency/Qt skips)**; Python 3.9 grammar **136 files**,
  `compileall` and `git diff --check` pass. After the combined suite, a real
  two-track FFmpeg movie UI test was strengthened to select its stream through
  the actual menu and the 16-test EXR/audio subset passed (1 dependency skip).
  These are automated checks, not proof of production OpenEXR binary bindings,
  a physical HDR GPU monitor, speakers in Maya, Maya 2023 GUI, real studio OCIO
  or authenticated enterprise ShotGrid operations.

### 24 October 2026 — connected precision, geometry, export and review pass

- A signed native EXR dataWindow is placed onto its verified displayWindow
  canvas in the actual Maya window, Source decoder, primary viewer and pixel
  inspection; out-of-data padding is never reported as a fabricated native
  HDR sample. Per-frame varying dataWindow sizes/offsets remain supported
  against a fixed signed displayWindow/PAR contract. Source/Working scopes
  can explicitly include the full display canvas or restrict to native data
  coverage. Real FFmpeg EXR and synthetic native OpenEXR window fixtures
  exercise signed origins, HALF/FLOAT/UINT precision and output guards.
- Live CPU source comparisons now rectify member pixel aspects and image
  extents into the reference Source's physical field, with native per-member
  original inspection using the same inverse sample coordinates. Transparent
  outside pixels and stretch-to-fit GRID retain their explicitly distinct
  semantics. The actual Maya comparison viewport now respects the reference
  PAR; unequal-PAR composite output remains an explicit queue refusal.
- For bounded supported image inputs, the current Export UI now confirms and
  freezes PAR, native postflip crop, horizontal/vertical flips and quarter-turn
  rotation. Working float EXR and Display PNG16 precision export use real
  linear-light resampling and correct alpha. Signed sparse frame lineage,
  exact geometry, output pixels, manifests, staging, reversible overwrites,
  cancellation and recovery remain bound to the ONE durable export queue.
  A new real Maya UI→durable QThread queue→FFmpeg encoded/decoded EXR test
  verifies this connection. Movie, Source legacy and unequal-PAR composites
  remain explicitly unsupported for this geometry plan.
- Review Records now edits/deletes only verifiably unsent local replies by
  stable reply IDs through actual Maya widgets. Optimistic frozen body and
  modified-time comparisons, locked/remote/conflicted note checks, safe
  disabled imported/synced reply controls and independent per-reply draft
  preservation prevent accidental publication/edit of remote records. The
  typed document remains undo/snapshot-friendly; no ShotGrid write occurs.
- The separate experimental GPU helper now gates actual Qt GL float texture
  upload/context initialization and maintains truthful CPU SDR fallback and
  color diagnostics. Hardware framebuffer, native HDR monitor and OCIO shader
  parity remain UNVERIFIED: the primary Maya review surface has not been
  replaced with an uncertified GL widget.
- During the first full Maya regression sweep, a previously introduced
  `ExportSpec` local-import shadow caused an unexpected headless modal
  error dialog. This real normal-export-path bug was isolated and fixed.
  Final full Maya tests were run with unexpected warning/error dialogs
  surfaced as failures rather than allowing nested QEventLoop hangs.
  **Prior-pass verification:** Maya 2024 `mayapy` **768 tests, OK (3 skipped)**;
  standalone host Python **765 tests, OK (307 optional skips)**; **143**
  Python 3.9 AST-compatible files, compileall and `git diff --check` PASS.
  No production Maya GUI/OpenGL HDR framebuffer/actual speaker/real enterprise
  ShotGrid endpoint was available. All migration edits preserved; no
  reset/clean/commit/push.

### 24 October 2026 incremental validation

- Review Records now supports local replies in its existing Discussion editor.
  Blank input does not mutate a note; locked layers retain drafts, note switches
  preserve per-note drafts, and adding a reply preserves unapplied main-note
  fields. IDs, replies and local dirty state survive session save/reopen.
- Canonical review capture now schedules recovery only if the underlying source
  review state changes; an unchanged capture or navigation cannot restart it.
- `SequenceSource` has tested **opt-in** sparse missing-frame presentation and
  explicit hole versus corrupt-file status. Player/probe/UI still use their
  existing dense sequence semantics until an intentional source-frame span
  setting can propagate through MediaInfo, FrameMapping and export.
- The ShotGrid browser rejects stale notes/versions and guards asynchronous
  publish refresh after changing Version or local Source. Declined confirmation
  cannot enqueue or publish; local acknowledgement remains tied to IDs and
  published content, not whichever Version happens to be visible later.
- Full Maya 2024 `mayapy` offscreen suite: **526 tests, OK (1 skipped)**.
  Host Python suite: **523 tests, OK (131 expected skips)**. Compileall and
  `git diff --check` pass. These are test counts, **not** live Maya/GL/audio,
  real ShotGrid server or color-managed output certification.

### 24 October 2026 — integrated media/color/export pass

- Media probing preserves an existing-files map independently of the selected
  playback timeline. `Playback → Missing Sequence Frames` switches one Source
  between legacy Skip, Hold, Black, Checker, and Error. Session restore, source
  changes, annotation source-frame identity and concatenated/montage composition
  use the same mapping; corrupt existing files remain decode errors, not holes.
- The live `PlaybackSurface`'s **main pixmap** now derives from source precision
  through working and display processing before 8-bit SDR encoding. EXR
  highlights and negative source samples are not lost before that transform.
  Source-owned Color Pipeline settings (input/working/display/optional OCIO)
  survive `.frsession`; an unsupported transform is visibly unmanaged/failed.
  The still separate `GPUMultiViewer` uses the same CPU reference display
  preprocessing where available, but live OpenGL/OCIO hardware parity is not
  demonstrated by headless Qt.
- The existing durable queue now has a **working** stage for float32 EXR and a
  **display** stage for supported image-sequence-to-movie/sequence outputs.
  Signed source frame maps and Hold/Black substitutions are recorded in the
  verified output manifest. Unsupported alpha, movie sources, tracking,
  burn-ins, nonstandard speed and incompatible hole policies are rejected,
  never silently labeled managed. Real FFmpeg fixtures validate negative/HDR
  float output and display-referred sRGB MP4/16-bit PNG; staged output is
  committed only after verification. Real OCIO studio config remains untested.
- New UI/geometry/decoder regression coverage checks source/frame identity,
  policy session reopen, composition timeline, Color Pipeline persistence and
  frozen managed export queue context. Reentrant Maya currentTime and delayed
  frameChanged Qt tests now cover scrub/read/write coalescing, reload and
  shutdown timer guards. Audio hardware, GPU display and ShotGrid permissions
  remain outside headless Maya verification.
- **Final combined run after integration and shutdown-guard fixes:** Maya 2024
  `mayapy`, offscreen Qt: **560 tests, OK (1 skipped)**; system Python without
  Maya Qt/NumPy: **557 tests, OK (157 expected skips)**. Ten dedicated Maya
  sync Qt tests passed (including the previously failing stale-frame signal
  during shutdown). `compileall` and `git diff --check` passed. Tests do not
  certify a native Maya 2023 viewport/audio driver or Flow studio permissions.

### 24 October 2026 — extended precision and authored sequence integration

- Explicit image-sequence cookie/range imports now route the authored pattern
  through media probing while storing the actual first available file for
  decoding. Session timing retains its authored timeline when reloaded from an
  existing source member, including empty leading/trailing/stepped positions;
  neighbors outside the user's declared range are not imported. Range parsing
  rejects more than one million frames before allocating memory.
- A present-but-corrupt PNG/EXR produces its own **Decode failure** frame with
  real signed source identity. The Player stops on verified corruption and
  clears the previous pixel/precision sample but preserves crop/annotation
  layers. Intentionally absent frames continue under their Hold/Black/Checker/
  Error policies without being mislabeled as codec errors.
- The EXR decoder can use isolated FFmpeg/FFprobe + NumPy FLOAT32 RGB(A) when
  native Python OpenEXR is absent; live mayapy tests generated real FFmpeg
  EXR files, decoded signed/negative/HDR values and verified the primary Player
  SDR preview receives the immutable float payload. The FFmpeg fallback has
  raster-only geometry and cannot recover original EXR per-layer channel names
  or precise data/display windows, unlike native OpenEXR.
- Precision comparison now supports per-member input profiles into a shared
  linear working comparison and one post-composite display transfer, with
  source-lineage preservation, alpha-aware Over and unmixed native float
  inspection. A no-NumPy/invalid-OCIO/software fallback is labeled explicitly;
  the independent `GPUMultiViewer` still lacks interactive workstation proof.
- Managed exports now include real EXR RGBA float, PNG 16-bit alpha, signed
  Checker/Error missing slates, transparent-format preflight and safety checks
  for corrupt real sources. The default source path is still the legacy
  unmanaged FFmpeg route. Alpha association can be specified per Source.
- The export queue's durable commit is now rollback-aware for both movie and
  sequence overwrites, retaining the existing public output through new output
  verification. Interrupted promotion, symlink paths and failed manifest
  validation are explicitly tested; no crashed job is silently retried.
- Flow/ShotGrid now offers user-mediated conflict inspection: read-only scoped
  Version/Note lookup, local vs remote evidence, explicit local ack/requeue/
  status-rebase/discard, a second remote/local recheck and independent consent
  before any later network publication. Concurrent changes, duplicate tags,
  disconnects and stale network results do not silently mutate another source.
- **Final combined validation for this implementation pass** (after the
  integrated decoder, comparison, managed output, queue crash recovery and
  ShotGrid conflict changes): Maya 2024 `mayapy` with Qt offscreen **614 tests,
  OK (1 skipped)**; system Python **611 tests, OK (188 expected skips)**.
  Python 3.9 grammar parsing succeeded for **125 Python files**, `compileall`
  and `git diff --check` succeeded. The existing dirty package migration was
  preserved untouched by Git reset, clean, commit or push.
- Real FFmpeg image assets were created and decoded inside the new regression
  suites (floating-point EXR RGB(A), PNG alpha, sparse negative frames and
  managed movie/sequence outputs); fake authenticated ShotGrid clients
  demonstrated read-only inspection and local-only conflict decisions. No
  running interactive Maya GUI process, speaker output, genuine OpenGL
  framebuffer, configured production OCIO view or real studio ShotGrid
  permission endpoint was available to certify these remaining gates.

## Existing foundation — do not rebuild

- `Sequence` and `FrameMapping`: patterns, holes, negative/source frames,
  rational FPS, and drop-frame timecode calculations.
- `Source`, `Representation`, and `Session`: stable IDs, alternate media,
  portable state, migrations, and unknown-field preservation.
- `PlaylistPanel`, `Composition`, and `CompositeSource`: live Sequence, Grid,
  Difference, and Inverted Difference for movies and sequences. Selection means
  selected clips; empty selection means all.
- `Player`, `FramePump`, `SequenceSource`, `RollingFrameCache`, and
  `CachePolicy`: playback, marked ranges, speed, audio sync, and bounded cache.
- `GPUMultiViewer` and `viewer.compositor`: GPU comparison foundation and a
  deterministic software fallback.
- Notes schema v2, propagation, merge, selectable object drawings, and directly
  editable text.
- `ImageGeometry`, `CropRect`, path mapping, capability probing, and burn-in
  template models.
- Current window workflows for drawings, tracking, timeline, thumbnails,
  waveform, scene persistence, export, and ShotGrid.

Completed behavior belongs in README/changelog, not this backlog.

## Mandatory integration rules

1. `Session` owns portable review state; `Settings` owns user preferences;
   `scene_state` owns Maya linkage and a compact recovery snapshot.
2. Display index, source frame, Maya frame, seconds, and timecode are converted
   explicitly through `FrameMapping` or a session time map.
3. Decode, interpretation, grade, display transform, comparison, overlays, and
   export form one declared pixel pipeline.
4. Transport/audio/cache extend `Player` and `FramePump`; no competing timer or
   playhead is allowed.
5. Comparisons extend `Composition` and `CompositeSource`; playlist widgets do
   not render or decode.
6. Playlist rows, notes, exports, and remote links use source IDs, never paths or
   row numbers as identity.
7. Optional dependencies are capability-gated and retain safe fallbacks.
8. Workers return owned buffers plus generation IDs. Qt, GL, and Maya objects
   remain on the main thread.
9. Every worker, timer, callback, decoder, and GL resource participates in
   `shutdown()` so `framereviewer.reload()` is safe.
10. File migrations are pure, retain unknown fields, and load existing data.

---

## 1. Live session ownership

**P0. Outcome:** playlist, current media, comparison, notes, crop, markers, and
representations become projections of one live `Session`.

### Integration plan

- Add a Qt-free `SessionController`; construct it before window UI creation.
- `_load(info)` reconciles a `Source.from_media_info`: stable ID first,
  normalized representation path second. Representation changes never create a
  new logical source.
- Change playlist rows from raw paths to source IDs. Keep a temporary `paths()`
  adapter, but composition consumes `effective_source_ids()`.
- Resolve a representation path only when creating its decoder.
- Move source range, playhead, markers, crop, flips, track reference, notes
  reference, and missing-frame policy into `Source.review`.
- Keep volume, layout, panel visibility, and user defaults in `Settings`.
- Migrate legacy `scene_state` into a Session in memory, then persist a session
  snapshot plus Maya linkage instead of independently scraping widgets.
- Add atomic `.frsession` Open/Save, dirty state, autosave, and recovery.

### Done when

Source switching restores its state; relinking preserves IDs; session and legacy
scene payloads round-trip; reload leaves no live callbacks or workers.

## 2. Unified source resolution and relinking

**P0/P1. Outcome:** every input becomes a representation of a stable source.

### Integration plan

- Route Open, drop, recents, notes, sessions, and ShotGrid through one Qt-free
  `SourceResolver` returning `Source + MediaInfo`.
- Accept files, sequence members/patterns, `pattern range`, folders, notes JSON,
  `.frsession`, LUT/CDL, and separate audio.
- Apply `PathMapper` before declaring media missing. Retain authored and locally
  resolved paths separately.
- Add representation preference: automatic/original/published/review/proxy.
- Display missing sources in the playlist and allow per-representation relink or
  folder search without changing source/note IDs.
- Expand `MediaInfo`: PAR, bit depth, alpha/premultiplication, layers/channels,
  color tags, timecode, data/display windows, file size, codec/pixel format.
- Make SequenceSource render Hold, Black, Checker, and Error hole policies.
- Folder watching may suggest a new representation but never silently replaces
  the reviewed version.

**Verified intentional-hole preview/delivery parity (24 October 2026):** A
single Qt-free bounded `media.missing_placeholder.raster_placeholder` now
constructs opaque Black, Checker and deterministic font-independent Error
slates. Real `SequenceSource` QImage/QPixmap and existing managed single-source
Display PNG16 QThread use those same u8 native raster values, with exact
`u8 * 257` 16-bit channels at native unscaled output geometry. Real Maya2024
offscreen primary GUI import and seek of signed `[-3,-2,-1,0,1]` sparse PNG
source through the existing Export UI/ONE journalled queue independently
decoded every output checker RGBA16 sample and verified each matches the
source pixmap, including opaque alpha and signed manifest hole identity.
Additional real Qt/FFmpeg tests cover leading/middle/trailing stepped holes,
RGB-only and nonopaque RGBA sources, corrupted present files rejected as
`decode_error` (never intentionally missing), no synthetic HDR payload and
preservation of existing artist output on failed queue render. Working EXR
placeholder float palette and geometrically/color-transformed composites are
not claimed pixel-equal to these unscaled Display rasters. Folder watching,
remote storage and native deep EXR remain separate acceptance gates.

### Done when

All entry points resolve consistently; sparse/negative frames survive; mapped
sessions relink cross-platform; missing media stays editable; hole policy is
visible paused and playing.

## 3. Shared geometry and navigation

**P0/P1. Outcome:** pixel aspect, windows, crop, transforms, overlays, and hit
testing use one coordinate system.

### Integration plan

- Make `PlaybackSurface` consume `ImageGeometry`, keeping aperture, data window,
  display window, viewer crop, matte, and export crop distinct.
- Create one image-to-view transform used by display, notes, text, crop,
  tracking, sampling, and GPU rendering.
- Preserve pan/zoom; add rotation, reset, Fit, 1:1, 2:1, nearest mode, and a
  zoom-dependent pixel grid.
- Apply orientation/PAR before overlays and store drawings in normalized source
  or display-window coordinates, never widget coordinates.
- Wire `CropRect` into viewer and export; add numeric fields, aspect presets,
  overscan, and normalized copy/paste between sources.

### Done when

Notes, crops, tracks, and samples align through PAR, flip, rotation, zoom, proxy
switching, and resolution changes; viewer crop/matte/export crop stay independent.

## 4. High-precision color pipeline

**P0 — highest fidelity gap. Outcome:** production EXR/color review is explicit,
inspectable, bypassable, and reproducible.

### Required pipeline

Decode at source precision → interpret alpha/orientation/windows/PAR → input
color transform → linear grade/CDL → comparison → display/view/look → channel
view → annotations/guides/burn-ins → output encoding.

### Integration plan

- Add `ColorPipelineState` to `Source.color`/`Session.color`: OCIO config and
  context, input space, display, view, look, exposure/gamma, CDL, LUT roles,
  alpha mode, and bypass flags.
- Convert current `viewer.color` controls into an editor for this state; migrate
  existing exposure/gamma/temperature into the grade stage.
- Expand capability reporting with OCIO version/config/GPU status. An absent
  config is visibly unmanaged/sRGB fallback, never silent equivalence.
- Introduce immutable `FramePayload`: owned pixels, dtype/channels, geometry,
  source ID/frame, generation, tier, and color metadata.
- Preserve 16-bit/half/float decode and cache payloads rather than lossy JPEG.
- Implement a CPU reference transform, then equivalent GPU shader stages.
- Compare in scene-linear by default when both sources are managed; allow an
  explicitly labeled display-referred alternative.
- Export Source, Working, or Display pixels with a transform manifest.

### Done when

Float values above 1 remain inspectable; CPU/GPU paths match within tolerance;
representation color conflicts warn; bypass exposes every stage; export records
all transforms.

## 5. Primary GPU renderer

**P0. Outcome:** one renderer handles precision, color, single/multi-source
display, while current graphics overlays remain interactive during migration.

### Integration plan

- Place `GPUMultiViewer` below a transparent overlay scene rather than rewriting
  annotation interaction at once.
- Feed `FramePayload` from Player/CompositeSource and upload only when source,
  frame, generation, tier, or processing changes.
- Add half/float textures, nearest/linear filtering, channel modes, and shaders
  for over, blend, absolute/inverted difference, wipes, split, and checker.
- Reuse `Composition.grid_cells()` for drawing and hit testing.
- Retain `viewer.compositor` as test/export reference and fallback. GL failure
  switches once with a diagnostic; do not recreate contexts repeatedly.

### Done when

Existing composition behavior is unchanged; overlays align at all device pixel
ratios; context loss/reload is safe; refresh avoids unnecessary QPixmap copies.

## 6. Pixel inspection and scopes

**P1. Outcome:** artists can inspect source or display values without ambiguity.

### Integration plan

- A `SampleService` converts through shared geometry and samples `FramePayload`
  before or after color.
- Inspector shows source/display coordinates, source frame, RGBA, HSV,
  luminance, numeric type, alpha mode, sample stage, and source tile.
- Add hover, locked and comparison samples plus magnifier.
- Store channel mode in `Session.view`: RGB/RGBA/R/G/B/A/luminance,
  false-color, gamut/over-range, and transparency checker.
- Compute histogram, waveform, and vectorscope on cancellable downsampled jobs,
  cached by generation and color stage. Label Source/Working/Display.

**Verified primary Image Inspection magnifier (24 October 2026):** The real
Maya View → Image Inspection menu enables a mouse-transparent floating 9×9
native-pixel grid with nearest-neighbor Display samples from the actual
`frame_item` QImage, not scene annotations or a second decoder. The exact
shared source→viewport transforms handle PAR, four quarter-turn orientations,
flip, zoom and pan; source-normalized pinned A/B samples remain independent.
An immutable original FramePayload (HALF/FLOAT/UINT) may supply precise raw
storage channels and original signed source ID/frame/absolute EXR position
alongside explicitly labelled 8-bit Display preview. No HDR value is invented
for dataWindow padding, missing source frames or decode errors. Generation
invalidation and read-only Qt callbacks prevent stale frame samples or any
Session dirty/autosave. Actual Maya2024 widgets, native HALF/FLOAT/UINT,
PAR/rotation/zoom/pin and clear/shutdown tests passed. This does NOT establish
GPU readback, physically calibrated HDR display, interactive Maya 2023 or
complete scope/proxy performance acceptance.

**Live multi-source inspection and generation fencing (24 October 2026):**
`CompositeSource.inspect_member_pixels(frame, uv, mode='visible')` now maps
actual cached compositor pixel *centres* to their visible contributors: selected
A/B or Flicker, raster-correct Wipe/Checker/Split/Grid, and independent source
operands for Blend/Difference/Over. The original two-argument `mode='all'`
inspection contract remains supported for existing analysis adapters. Every
native sample retains its true source/occurrence identity, signed frame,
HALF/FLOAT/UINT storage and source/input color interpretation; a separately
declared straight/premultiplied association invalidates both reader and
comparison cache generations rather than reusing old pixels. No alpha
association is inferred from EXR channel count as independently proven. A
sparse Hold displays genuinely cached previous source pixels with the real
held-from signed frame and distinct requested missing label; an evicted source,
Black/Checker/Error or decode failure cannot acquire fake native precision.
The actual Maya primary Inspector requires Player's source/frame/QPixmap
`framePresented` receipt before attaching such native labels to the current
viewport. Actual Maya tests cover rapid seek, mode changes, unequal PAR,
native signed EXR, A/B pinned points and read-only unsaved Session behavior.
This does not establish user-configurable per-pixel inspector channels,
physical GPU monitor precision or production hardware frame latency.

### Done when

Samples survive all transforms and identify grid tiles; scope jobs never block
playback and cancel safely on load, close, or reload.

## 7. Full comparison workflow

**P1. Outcome:** fast version comparison beyond current sequence/grid/difference.

### Integration plan

- Extend `Composition` with A/B, Flicker, Blend, Wipe, Split, Checker, and Over;
  store parameters in `Session.view`, edit them from PlaylistPanel.
- Add per-member source in, session offset, rational rate, hold, and trim mapping.
- Reuse CompositeSource scheduling/cache aggregation—never add another player.
- Keep A/B sources warm; switching visibility must not rebuild decoders.
- Add frame/timecode/manual alignment plus spatial offset/scale and clear visible
  source labels.
- Add active tile labels and linked/independent grid navigation.
- Retain selection/all semantics; validate two-source modes and report ignored
  extra selections.

**Connected compositing/managed delivery increment (24 October 2026):**
The CPU Working compositor now samples at exact destination pixel centres,
performs each Source→Working interpretation before a single needed resize,
and treats native width/height/PAR separately from decimated preview raster
size. Unequal-PAR overlapping sources are placed in the first member's
physical image field; invalid outside coverage and zero-alpha hidden RGB stay
transparent. Grid deliberately samples each member once into its own native
canvas cell (no redundant full-frame downsize). Straight/premultiplied Over
retains signed negative and over-range float values with fractional alpha.
The Maya GUI visibly selects Wipe, Checker and Grid native contributors while
its independent v2 managed comparator writes frozen square-pixel Working
float EXR and Display SDR PNG16 via the ONE pre-existing durable queue.
Real FFmpeg-generated signed-frame float EXR with 3/2 and 1/2 PAR passed
primary cached Working-to-independently-decoded EXR RGB/alpha checks and
independent Display 16-bit sRGB/alpha quantization checks. Direct single
Source loads clear stale comparison state even if that Source belonged to the
former comparison; explicit direct composition constructors still retain
their own Source IDs and rational mixed-CFR mapping. **Unequal-PAR A/B and
Flicker** now use a bounded Working float rectification of the actual selected
native plate into the first Source physical image field, with exact native
Source-pixel ownership. The current viewer/queue contract is independently
verified for real FLOAT EXR signed labels, negative/overrange RGB, straight
fractional alpha, 3/2 vs 1/2 PAR and actual Maya GUI→durable Working EXR.
A queue job only proceeds after a full reference-resolution Working frame is
genuinely presented and the exact Source/frame/QPixmap receipt matches; Qt-only
fallback, missing native samples, downsampled or stale images cannot publish.
For native EXR v2 Flicker, an explicitly frozen positive integer 1–120-frame
period selects member A/B independently for every **absolute session frame**;
real Maya GUI frames 0 and 1 matched the separately decoded first/second
Working EXR output frames with straight fractional alpha and distinct signed
source labels. Forged period values, inappropriate `active` overrides, and
legacy v1/mixed-movie v3 Flicker delivery reject before rendering.
Equal PAR never excuses *different native width/height* in selected A/B/Flicker:
the source rectifies any unequal (width,height,PAR) into the first physical
reference field while the window insists on the exact presented Working
precision/source cache/provenance receipt before v2 delivery. Real Maya 16×8
vs 8×8 FLOAT EXR plates both declaring PAR3/2 now pass selected B native
inspection, 24×8 Working float and Display PNG16 delivery through ONE queue,
both independently decoded for alpha, negative HDR, clipping and scene-linear
blue .25 vs Display sRGB ~.537. A saved/reloaded A/B Session with B8×4
preserves logical source IDs, chosen B, signed frame and reference canvas.
Native quarter-turn/flip/crop preserve the Qt affine model but still reject
managed composite export because baking those effects is not verified.
GRID is intentionally square/stretch
for mixed AND equal non-square PAR; numerical `1` and `1/1` PAR match.
This is an intentional supported-subset limit, not a claim of GPU, general
mixed movie PAR, advanced transitions or real HDR display certification.

### Done when

All modes share one playhead, align different rates predictably, preserve cache
and notes when switching mode, and support movie/sequence/mixed inputs.

## 8. Transport, timing, and cache

**P0/P1. Outcome:** playback is deliberate and measurable under Maya load.

**Connected player generation-safety improvement (24 October 2026):**
The primary Maya `Player` now fences synchronous and queued EXR/Qt image
results by the actual media-generation, Source object, requested frame and
monotonic request serial before and after `get()`/`get_payload()` and before
`framePresented`/navigation completion. CacheChanged/FramePump callbacks can
re-enter and replace the active source while decoding; old pixmaps/native HDR
payloads can no longer publish over that replacement. Rebuilding a composition
also resets the initial `frameChanged` receipt at frame 0 even when a previous
source occupied the same playhead. Signed sparse Hold-origin search is bounded
logarithmically and shutdown prevents a closed `SequenceSource` from
restarting decoder work. Real FFmpeg signed EXR color fixtures, deliberate
decode failures, rapid Qt queued seek/source replacement and worker shutdown
are covered by the new Maya tests; physical speaker synchronization and
large studio-load timing remain explicitly unverified.

**Selected-source original-content freeze (24 October 2026):** An opt-in
image-only SHA256 verifier binds the actual decoded original's canonical
no-symlink path, inode, size, mtime/ctime, signed source frame and original
full-file hash to the bounded precision cache. Two full-file hashes surround
decode (at most 512 MiB, streamed), with an independent cache-only original
verification before the Maya Window may freeze a managed physical comparison.
Warm BLEND/GRID→A/B/Flicker switches purge unproven cache entries before
seeking. Same-size/matched-mtime in-place edits or atomic file replacements
invalidate both displayed/native caches and **latch that old comparison into
export rejection** even if Qt later redecodes newer data; explicit composition
reload with newly presented proof is required. Hold only attributes the real
earlier original if cached; no fabricated proof for gaps, error slates, offset
windows, missing or evicted pixels. This does not claim atomic immutable
repackaging of every source byte at the instant the artist inspects it; the
existing durable export queue independently enforces its own frozen input
hashes and recovery receipts. Normal playback remains un-hashed by default.

### Integration plan

- Add signed speed and Once/Loop/Ping-Pong to FramePump; make cache candidates
  direction-aware.
- Implement J/K/L through existing `Player.set_speed`; reverse audio defaults to
  muted, with optional scrub snippets.
- Use rational FPS in deadlines, seeks, audio offset, composition, and Maya sync.
- Add go-to display/source/Maya frame, seconds, or timecode.
- Diagnostics: presentation/decode FPS, latency/jitter, skipped/repeated frames,
  cache hits, underruns, A/V and Maya drift, decoder, tier, and pixel format.
- Timeline cache states: queued/decoded/missing/failed/evicted. Add Purge, Cache
  Marked Range, and Cache All.
- Add proxy scrub, display playback, and source paused tiers; cache keys include
  tier and export always requests source.
- Hardware decode remains capability-gated and benchmark-driven.

### Done when

Reverse/ping-pong respect ranges; rational clips remain synchronized; cache UI
matches reality; tier changes preserve frame identity; diagnostics explain loss.

**Record transport follow-through (24 October 2026):** For imported and
explicitly authored SEQUENCE record timelines, `Composition.record_frame_address`
now returns exact per-edit occurrence, rational media decoder index and
held/outside-trim/record-gap states without asking any decoder to guess an
original negative source frame. The actual Maya `CompositeSource` resolves
signed EXR/PNG labels, reportable source holes and failed decodes in one shared
Viewer/Player clock. Out-of-range record reads cannot clamp to the final edit;
late asynchronous callbacks are ignored after closing/reloading the source.
These are independently regression-tested with CFR FFmpeg movies and native
signed sparse plates. Production-length cache/performance and interactive Maya
2023 playback remain unverified.

## 9. Audio review

**P1. Outcome:** channel layout and synchronization are explicit.

### Integration plan

- Preserve actual source layout in MediaInfo/Source. The timeline waveform stays
  mono only as a visual overview; this does not make playback mono.
- Attach external audio with path, stream, rational interpretation, sample rate,
  and signed delay through the common resolver/path mapper.
- Extend Player with stream selection, delay, mute/solo, output layout, meters,
  and sync telemetry—no parallel playback controller.
- Add cancellable/throttled scrub snippets.
- Document downmix matrices for mono, stereo, 5.1, and unknown layouts.
- Sequence mode switches/crossfades at clip boundaries. Multi-image modes use an
  explicitly selected audio source; never mix every source implicitly.
- Store source delay/stream in `Source.audio`; output device/layout and scrub
  preference in Settings.

**Progress (24 October):** Source-owned stream and signed delays are connected
to the one Player. Native Qt A/V is not used for independently offset sound;
live changes preserve trim/position/loop state through split FFmpeg video + Qt
sound playback. Rational frame-to-audio seeks, delayed loop restarts, selection
capability reporting and read-only Playback → Audio routing/sync diagnostics have
focused real-movie and Maya tests. No physical audio device calibration,
sample-accurate hardware guarantee, Qt5 channel DSP or timeline crossfades.

**Additional bounded external record soundtrack delivery (24 October):** The
v4 hard-cut, single-queue PCM plan can now freeze separately proven zero-origin
external 48 kHz stereo s16 RIFF/WAV, with exact source hash, stream ID, decoded
PCM hash and sample count, distinct from the already supported selected embedded
movie tracks. Original source video decoder indices or image local-frame indices
are mapped by exact `24000/1001` rational sample boundaries, including repeated
edit occurrences, cuts and zero-valued PCM samples across record gaps. Maya's Export-only **Audio
per edit…** dialog permits different tracks/files per occurrence; changing its
choices does not modify the saved Source's playback sound or autosave a review.
Original selected PCM and staged/promoted WAV samples are independently decoded
and compared by the same durable queue including crash/retry. External playback
offsets, arbitrary WAV metadata, compressed files, unverified start time,
resampling/retiming, image/video sync inferred from filenames, and live
speaker switching continue to reject or remain unimplemented.

**Verified offline record waveform and optional Session editorial intent
(24 October 2026):** The Maya Export panel now offers a separate user-confirmed
`Verify record waveform…` action. It freezes the same supported v4 audio plan,
then a generation-safe cancellable QThread reconstructs original embedded or
canonical external PCM *without publishing a file*, confirms source SHA/PCM
identity and derives one exact left/right/peak amplitude bin per signed edit
occurrence and `24000/1001` record frame. Native timeline waveform painting
has a special per-frame mode: real silent gap bins remain zero rather than
receiving misleading interpolated audio from their neighbors. Verified record
source/stream/sample-boundary details follow the actual Maya playhead and are
read-only: this inspection does not require device playback or imply speaker
latency proof. Selecting **Remember these
per-edit sound choices in the review Session** is an explicit true document
mutation and can be saved/reopened with a typed exact edit fingerprint; stale
edit lists, invalid saved metadata or changed sound files do not inherit a
seemingly valid track. Export-only intent, normal seeking, worker completion and
waveform visualization do not schedule review autosaves. Separately enabled
Qt5 record PCM audition now uses the same independent proof and the video
playhead as its master; physical device onset/drift remain uncalibrated.
Exported transparent gaps remain visually opaque black in the Qt preview.

**Additional bounded record delivery (24 October):** An opt-in Export checkbox
now freezes an independently selected verified embedded PCM 48 kHz stereo track
for each v4 SEQUENCE/EDL/OTIO hard-cut occurrence, or explicit silence for
image-only cuts. Only unchanged zero-origin 24000/1001 CFR video with 1:1 edit
timing is accepted, so every record frame maps to 2,002 PCM sample frames.
Repeated logical Sources may use separate occurrence routes, source trims map
to original audio decode indices, and empty record frames become exact zero
samples. Real FFmpeg output includes `record_audio.wav` inside the **same**
staged/promoted EXR/PNG directory; a five-frame edit produces 10,010 sample
frames including verified 2,002-sample silent gap. Every staged/promoted/
recovered WAV is compared with independently decoded original selected samples
and frozen source SHA, including artist overwrite rollback, cancellation,
receipt tampering and simulated interruption. Qt playback still cannot promise
live crossfades or calibrated physical speaker timing. Proven canonical
zero-origin external PCM WAV and bounded Display-only MOV/NUT mux are now
supported explicitly, not as arbitrary external offsets/compressed codecs;
independent sample-unaligned delay, unverified audio, arbitrary retime and
scene-linear HDR movie still reject.

**24 October 2026 — one authored overlapping audio-only edit:** Maya Export
now offers **Audio-only record layer…** with independent canonical WAV source,
`record_start_frame`, `source_in_frame`, `duration_frames` and exact constant
gain 0/half/unity. This is an actual additional editorial occurrence, not a
variant of a video cut or a guessed default media track. The explicit overlay
overlaps independently selected per-cut soundtrack and can fill *only* the
record-gap frames it really covers; otherwise picture gaps remain sample-exact
silence. Signed stereo PCM16 is added with independently tested toward-zero
rational gain and saturating overflow at precisely 2,002 samples/frame for
`24000/1001` record to 48,000 Hz audio. Original external SHA, WAV samples,
selected source and final mixed stream are frozen and independently validated.
One underlying mixed PCM buffer feeds async read-only waveform bins (including
`mixed` and `overlay` kinds), fake-device Qt5 video-master QBuffer seek/play,
durable WAV and optional MOV/NUT. Actual Maya2024 offscreen GUI tests compared
every mixed sample and verified audible authored gaps, zero unsolicited
autosaves, source/edit invalidation and exact QBuffer sample offsets. Session
Remember stores fingerprinted intent, not untrusted media-provenance proof;
EDL/OTIO warns and obtains consent instead of claiming to carry audio-only
overlaps through interchange. Arbitrary multi-layer, dynamic crossfades,
audio DSP and physical device synchronization remain unmet acceptance gates.

**Further connected integration — bounded ordered layers (24 October 2026):**
The existing single overlay remains immutable v1; a separate native grey Maya
**Manage ordered audio-only layers…** editor now permits ONE to THREE explicit,
reorderable external PCM events. Each separately records its canonical original
WAV path, source in-frame, record offset, duration and exact gain 0/half/unity.
The authored row order is part of the frozen edit identity because individual
signed PCM16 additions saturate before the next event: reordering can change
clipped sample values. Existing source-specific logical IDs and repeated picture
cut occurrence IDs remain distinct from unique audio occurrence IDs. Native
48k PCM/`24000/1001` requires exactly 2,002 interleaved stereo sample frames
per record frame, even over gaps and multiple overlapping authored events.
Every source is independently hash/stream/PTS/PCM-proven before and after its
mix; stage, promoted WAV, Display RGB8 MOV and lossless straight RGBA16 NUT
use the same final independently reconstructed PCM and immutable receipts.
The read-only Maya waveform lists all active overlay occurrences and accurately
distinguishes per-cut `mixed`, gap-only `overlay` and true silent gaps. Qt record
audition remains video-master and never certifies physical device latency.
Export-only changes do not mutate Session; Remember stores strictly validated
v2 exact-edit-bound ordered *intent*, retaining saved v1 single-event support.
Stale video cuts or edited path/clock/intent metadata refuse; EDL/OTIO demands
informed VIDEO-only lossiness consent rather than dropping authored music
silently. The exact ordered 1..3 subset is meaningful multitrack progress,
**not** arbitrary-depth mixing, native sample-offset editing, keyframed gain,
crossfades, surround or lossless audio-only EDL interchange.

### Done when

Mono/stereo remain unchanged unless explicitly routed; external audio survives
seek/loop/representation swaps; routing is visible; scrubbing reloads safely.

## 10. Notes as review records

**P0/P1. Outcome:** stable annotation identity plus review discussion/status.

### Integration plan

- Replace long-lived `{display_frame: strokes}` ownership with a projection of
  schema-v2 records, adapting at PlaybackSurface during migration.
- Resolve every edit to `source_frame`; permit negative frames.
- Preserve note/shape/source IDs on edits. Copies get new IDs and `derived_from`.
- Populate dimensions, PAR, windows, and coordinate space from ImageGeometry.
- A review note owns body, category, priority, status, assignee, author/time,
  replies, remote link, and vector/still attachments. Migrate drawing-only files.
- Add ranges/layers with author/category/status filters and lock/solo/hide.
- Add propagation UI around current pure functions and dirty-ID conflict choices
  around current merge logic.
- Add callouts, lines, arrow variants, distance/angle, font/alignment/background;
  all remain selectable transformable objects using existing undo.

### Done when

Negative source-frame notes survive edits; IDs/unknown fields persist; geometry
survives representation/PAR changes; remote refresh cannot overwrite dirty data.

## 11. Editable tracking and stabilization

**P1/P2. Outcome:** tracking is correctable and diagnosable.

### Integration plan

- Store normalized track keys, algorithm/version, source geometry, confidence,
  and matrices in `Source.review`; migrate existing centers/quads.
- Extend TrackingJob with backward/bidirectional operation and generation safety.
- Add manual keys, interpolation, smoothing, correction/re-track, and undo.
- Return feature count, inliers, forward/back error, confidence, and lost state;
  show quality in viewer and timeline.
- Treat tracked crop, stabilization, and tracked notes as separate consumers of
  shared tracking data. Export evaluates the identical transform model.

**Earlier progress (24 October):** Source-frame signed keys, validated per-source
geometry/orientation/rational rate, bounded tracked centers/quads and real LK
confidence/loss observations persist in review sessions. The Maya Crop/Track
panel displays actual observations and accepts a single manual translation
correction with undo/redo. FFmpeg B-frame CFR frame-index seeking was measured
against decoded pixels, and known lost frame motion refuses export. Manual
perspective/multi-key corrections, reverse tracking, broad stabilization,
full transform/export parity and live Maya2023 GUI stress remain open.

**Managed stabilization follow-through (24 October):** Source-owned Qt preview
now has a separate, explicitly confirmed and queue-managed **Working float32
EXR** deliverable. For up to 60 original signed source-frame labels, the
selected interval must include the unchanged preview anchor and use the exact
native translation plan frozen with original tracking quality, Source ID, FPS,
color, frame-to-file map, alpha and every source EXR SHA256. Actual FFmpeg
float32 input and output are independently decoded and compared per pixel,
including negative/>1 RGB and true transparent out-of-bounds alpha. The same
serial durable queue stages, validates, commits, rolls back, retries and
recovers this output; forged EXR with adjusted SHA or unreceipted extra files
cannot claim success. Only canonical full-window square-PAR linear EXR with no
OCIO grade, rotation, retime, missing plate, perspective/affine or manual/weak
track is enabled. No claim of general stabilizer, preview crop parity, Display
PNG, soundtrack/notes burn-in or physical GPU/HDR monitor certification.

### Done when

Tracks can be corrected, lost sections are visible, representation changes do
not break them, and viewer/export produce identical transforms.

## 12. Maya production context

**P1. Outcome:** scene/source relationships are visible and useful.

### Integration plan

- Central Maya adapter reports scene FPS/color, camera/Sequencer shot, Maya
  frame, sync ownership, and offset; widgets do not query Maya independently.
- Warn on Maya/source FPS and color mismatch.
- Extend Playblast Current Shot to Build Sequencer Playlist: stable Source per
  shot with camera, Maya/source ranges, and playblast representation.
- Detect stale playblasts and rebuild only changed shots.
- Notes may store camera, Maya frame, and object IDs; Jump To Scene restores
  context and optionally selection.
- Formalize FrameReviewer Master, Maya Master, Follow Only, and Disconnected to
  prevent callback feedback loops.

### Done when

Frame mappings are explicit/reversible; shot boundaries/cameras persist; sync
never loops; missing scene objects degrade gracefully.

## 13. Production-safe ShotGrid

**P1. Outcome:** secure, resilient, ID-based remote review.

### Integration plan

- Replace stored script secrets with Toolkit/Desktop user authentication,
  environment injection, or a studio provider; never persist secrets.
- Separate auth, query, representation resolver, and mutation/outbox services
  while retaining ShotGridPanel as UI.
- Add schema mapping, pagination, bounded retry/timeout, cancellation, and stale
  generation rejection.
- Resolve entities into existing Source/Representation objects with stable
  remote IDs; representation changes preserve review state.
- Durable idempotent outbox for notes/status/attachments, with confirmation
  before initial remote writes.
- Reconcile notes through `notes.merge`, expose conflicts, and apply PathMapper
  plus representation policy to media choices.

### Done when

No secrets persist; offline retries do not duplicate writes; refresh preserves
local edits; session round trips retain ShotGrid identity.

## 14. Export and presentation

**P0/P1. Outcome:** output reproducibly matches the chosen review state.

### Integration plan

- ExportJob consumes an immutable specification derived from Session, Source,
  FrameMapping, geometry, track/crop, color, notes, and BurnInTemplate.
- Render burn-ins into movies, sequences, stills, GIFs, contact sheets, and PDFs
  with preview and editable presets.
- Choose Source/Working/Display output and write a sidecar manifest containing
  transforms, IDs, range/FPS, crop, and note selection.
- Snapshot/Copy Frame can select composed view, overlays, notes, burn-in, crop,
  and display transform.
- Persistent sequential queue with progress, state, retry, logs, reveal/open,
  cancellation, and restart recovery.
- Probe completed output for frames, rational FPS, dimensions, audio, duration,
  codec/pixel format before success.
- Presentation mode selects monitor/transform and hides chrome/cursor safely.

### Done when

Output matches its declared viewer stage; burn-ins use source identity; failed
verification cannot report success; jobs terminate safely on close/reload.

## 15. Playlist, timeline, and interchange

**P1. Outcome:** large sessions remain understandable and navigable.

### Integration plan

- Playlist rows show thumbnail, role, version/status, range/FPS, missing/dirty
  state, and tags. Add reorder, groups, search, and filters.
- Reorder mutates `Session.sources` and immediately rebuilds Sequence segments.
- Timeline gains zoom/pan, Fit Session/Marked Range, and expandable source,
  notes, cache, tracking-quality, thumbnail, waveform, and boundary lanes.
- Map positions through a visible time window rather than full-duration width;
  cluster dense notes without changing their frames.
- OTIO/EDL adapters create sources/time maps, preserving trims, linear retimes,
  freezes, metadata, and alternates while reporting unsupported transitions.

**Record timeline preview alignment (24 October 2026):** The original primary
Qt record viewer now displays the actual edit occurrence, signed Source frame,
verified decoder index, trim state or true record gap at the moving playhead.
Source PCM waveform bins map to the exact same record frame positions, not to
an inferred global file basename. Repeated Source IDs keep independent cut
positions and optionally saved audio editorial intent without being silently
flattened. Unsupported imported audio-only tracks, OTIO transitions and
retimes remain blocked by the verified media delivery gate. An explicit
per-occurrence saved record-audio selection triggers a lossy-omission consent
gate for CMX/OTIO export, rather than masquerading as exported synchronized
sound; an edit-list/audio fingerprint mismatch refuses the interchange.
Lossless complex OTIO, audio-only edits and transitions remain unavailable.
The separate opt-in bounded Display RGB8/PCM `record_av.mov` is a verified
media derivative in the SAME durable PNG/WAV folder, not a lossless complex
OTIO interchange or HDR/alpha-preserving master.
An additional `record_rgba16.nut` FFV1 straight-alpha 16-bit Display SDR/PCM
derivative can be selected alone or together with MOV; the one queue verifies
both against original source SHA, Display PNG16 and the exact same selected
mixed stereo WAV. NUT's 1/48000 stream time bases represent every video PTS
at increments of 2,002 exactly; unlike an observed MKV stream with 1/1000
time base this avoids millisecond quantization of NTSC frame timestamps.
Full output RGBA16 and PCM samples are independently decoded and compared.
This is not an EDL/OTIO audio-only-track export, an HDR/linear movie or a
guarantee of universal NUT playback support.

### Done when

Reorder changes playback immediately; zoom never changes identity; dirty/missing
state is visible; interchange reports every lossy conversion.

## 16. Architecture, diagnostics, and extensions

**P0/P1. Outcome:** features can grow without enlarging the two UI monoliths.

### Integration plan

- Incrementally extract session/source, transport, Maya sync, annotation,
  crop/tracking, export queue, and panel controllers from `window.py`.
- Split `surface.py` into shared transform, image layer, overlay scene,
  annotation interaction, crop interaction, and sampling components.
- Define narrow probe/decode/audio/cache/color/tracking/publish/export interfaces;
  current classes are first implementations, avoiding a rewrite.
- Use immutable FramePayload and generation rejection across async boundaries.
- Grow `tool_registry` into versioned commands/tools/menus/shortcuts/overlays/
  importers/exporters/providers with duplicate-ID checks and remapping.
- Structured, bounded, redacted logs for source, decode, playback, cache, audio,
  color, sync, export, and integrations.
- Help → Capabilities/Support Report combines capability probes, session
  diagnostics, Maya/Qt/Python versions, recent logs, and remedies.
- Improve non-modal errors, retry, crash breadcrumbs, hotkey help, tab/focus,
  scalable themes, and first-run guidance.

### Done when

Controllers test without the full window; stale async results cannot reach dead
widgets; reports leak no secrets; extensions need not edit `window.py`.

## 17. Validation plan

### Pure tests

- Session migration/relink/recovery; every time domain including negative and
  NTSC/drop-frame; mixed-rate composition; color order and CPU reference.
- Notes v2 edits/ranges/layers/conflicts/IDs; geometry/PAR/windows/rotation;
  bidirectional cache scheduling and tiers; export specs/manifests/verification.

### Generated fixtures

- Mono/stereo/5.1/multiple streams/external offsets; 23.976 through 59.94 DF/NDF;
  sparse negative sequences; odd sizes/orientation/PAR/alpha/windows; integer,
  half and float color; corrupt/VFR/unsupported files.

### Maya lifecycle tests

- Reload with active playback, cache, waveform, thumbnails, tracking, scopes,
  export, ShotGrid, and GL; scene callbacks; sync modes; context loss; repeated
  scrub/source switch/recomposition/note edits.

### Performance gates

Track decode/presentation FPS, cache hits, memory, seek latency, texture uploads,
A/V drift, and shutdown time. Fidelity may never silently drop tier or precision.

## Recommended implementation order

1. **State and identity:** live SessionController, ID-based playlist, resolver,
   notes v2/ImageGeometry wiring, `.frsession`, recovery.
2. **Trustworthy pixels:** FramePayload, precision decode/cache, OCIO CPU path,
   primary GPU renderer, inspection/scopes, managed export.
3. **Review interaction:** A/B/wipes, reverse/J-K-L, cache controls, audio,
   timeline/playlist improvements, note layers/conflicts.
4. **Production delivery:** Maya Sequencer, secure ShotGrid/outbox, burn-ins and
   export queue, corrected tracking, OTIO/EDL, studio hooks.
5. **Optional:** benchmark-justified hardware decode, stereo sources, live
   sources, remote presentation, deep/multipart EXR.

## Definition of done

Every item must use shared session/source/time/pixel models; support movies and
sequences where relevant; expose tested fallbacks; survive source switching,
save/load, scene restore, and reload; shut down safely; include pure and Maya
lifecycle coverage; update README/changelog; and be removed from this backlog.
