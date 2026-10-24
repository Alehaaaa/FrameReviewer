# Changelog

## Unreleased

- **New connected production reliability increment (verification below):**
  Maya Session reopening now reconstructs saved record-timeline OCCURRENCES,
  not just the unique Playlist Source rows. Two differently trimmed cuts of
  one logical EXR Source, signed frame origins, transparent record gap,
  per-edit identity and explicit alpha survive clean Session save/reopen;
  a reopened Maya actual Export UI→ONE durable queue→independently decoded
  float Working EXR agrees with the fresh live picture. Reopening alone does
  not dirty Session, start autosave or insert generated media into Recents.
  The SHA-proven native v4 record preview now additionally detects when the
  **currently presented** original source bytes change before queue preflight,
  including same-size/restored-mtime modifications. A changed file remains
  export-blocked under that comparison even if restored until the artist
  explicitly rebuilds the composition and freshly presents it. Only the
  actual reviewed frame receives this preview-proof claim; durable delivery
  independently hashes and validates every source/output range member.
- **Reentrant SequenceSource reliability at larger signed sparse workloads:**
  Revision/closed-state fences suppress old QPixmap/native FramePayload/SHA
  caches and late Qt frameAvailable signals if shutdown, policy or color
  changes happen synchronously inside decode/hash/render/notification.
  Proven 100,081-frame sparse cache policy switches and holder lookups avoid
  traversing every missing position. Oversized Qt-only placeholder raster
  dimensions are clamped to the existing bounded cache memory contract;
  synthetic Black/Checker/Error remain display-only with NO native HDR.
  Worker Maya2024 actual EXR/FFmpeg: 11 new tests and 85 combined tests PASS;
  prime's related Recents/Session/record-SHA/EXR/Player/PCM combined focused
  Maya regression: 84 PASS before complete suite.
- **Recents persistence cleanup made lossless for genuine user opens:** The
  artist action now records a separate QSettings provenance marker. Opening
  the Recent menu prunes *only vanished, old test-generated `fr-*` artifacts*
  lacking that marker, not a genuinely opened temp file or off-line network/
  removable medium. Other missing entries remain ordered and visibly disabled
  until their media returns; no stale modal pops up and no history is discarded.
  Legacy actual QA pollution was previously removed selectively; entire Maya
  regression continues to isolate preferences in a disposable IniFormat store.
- **Latest measured integrated verification:** Maya2024 native `mayapy`
  offscreen `tests/run_maya_suite.py` **1,230 tests OK, 3 skipped in
  440.657 seconds**; host Python **1,120 OK, 545 expected skips**;
  Python3.9 AST **216 files PASS**; `compileall` and `git diff --check` PASS.
  The suite used a disposable isolated QSettings store; a separate inspection
  of the artist's real preferences after the suite confirmed that no QA media
  paths returned. No second export queue, Git commit or ShotGrid publication.
  Maya2023 interactive, hardware GPU/HDR/OCIO, physical audio and network
  storage/studio permissions still require independent production evidence.
- **Open Recent now reflects genuine artist actions instead of test fixtures.**
  The shared internal `_load` previously inserted *every* media path into the
  actual user's macOS QSettings history, including headless temporary test
  image sequences and composition placeholders. Open File, Import and viewport
  drag/drop now explicitly opt in to recently opened media; scene/composition
  restore and programmatic loads do not register fake user actions. The Maya
  suite now uses an independently isolated disposable IniFormat QSettings
  store for its entire run; even legacy VideoCropper preferences are not read
  into that test environment. The real user history was repaired by deleting
  only identified temporary `fr-*` fixture records, preserving genuine opens.
  Refreshing the Recent menu syncs preferences changed by another process.
- **Native signed-window EXR → actual primary Maya canvas:** A new bounded
  CPU scanline reader independently checks the file's absolute signed chunk
  Y and original channel bytes for single-part RGB(A) HALF/FLOAT/UINT in
  NONE/ZIPS/ZIP compression, including 32-bit unsigned values beyond 24-bit
  integer float precision, negative/over-one HDR, alpha and displayWindow
  padding. Maya's MediaInfo probe permits native offset canvas placement only
  after decoding genuine source pixels that agree with the frozen header;
  guessed FFmpeg window positions never authorize a native review. Malformed
  or unsupported native data fail closed. The existing managed Export refuses
  noncanonical offset-window frames until separately verified output semantics
  are available. Real Maya input/viewport sample and negative-Y rejection are
  exercised, without OCIO/GPU/HDR monitor or broad multipart/deep claims.
- **Authoritative record edit-picture parity:** Bounded opt-in v4 native EXR
  sequence preview now produces the same Source→Working float→Display color/
  alpha raster as the existing managed durable queue, with transparent RGBA
  zero for true record gaps rather than opaque Qt black. Repeated saved cuts
  now retain real logical Source IDs even when multiple edit occurrences point
  to one original path; stable `record-edit-*` occurrence IDs remain distinct.
  Full-source native pixels, signed labels and original frame association stay
  inspectable, while placeholders and unsupported Qt-only inputs never claim
  recovered HDR. Real Maya GUI→Export UI→ONE queue→independently decoded
  Working EXR/Display PNG16 tests cover exact record index, fractional alpha,
  signed HDR and gaps. Legacy unsupported preview modes are unchanged.
- **Previous measured verification for the geometry, transport and content
  integrity increment:** actual Maya2024 `QApplication` offscreen suite
  **1,210 tests OK (3 skipped; 469.673 s)**, host **1,100 OK (525 optional
  dependency skips)**, Python3.9 AST **211 files**, `compileall` and
  `git diff --check` PASS. Earlier suite failures were investigated and
  corrected rather than ignored: a true record PCM seek/FramePump serial
  regression, a test-import identity mismatch caused by `test_reload`, a
  macOS `/var`→`/private/var` original-file alias, and a warm compositor
  integrity-mode transition. The older 1,185/1,075/208 evidence is historical
  baseline, not this final regression. No production Maya2023 interactive,
  hardware HDR/OCIO display, real acoustic sync, authenticated ShotGrid or
  Windows/Linux/network-studio release gate is claimed.
- Added **cached-original SHA256 preview/export integrity** for selected
  physical A/B/Flicker on native EXR. Opt-in `SequenceSource` double hashes a
  bounded regular original around decode, pins its canonical symlink-free
  path/inode/mtime/ctime and keeps the proof with the original source
  FramePayload, never with synthetic placeholders, missing labels or evicted
  pixels. Cache-only `cached_file_proof` and bounded independent
  `verify_cached_file_proof` never re-decode, and expose actual signed Source
  labels. Switching a *warm* Blend/Grid into selected A/B/Flicker invalidates
  earlier unproven caches before re-presenting. The Maya Window's actual
  presented Source/frame/QPixmap geometry gate also verifies the currently
  selected original SHA and its correct logical Source/sequence path; a changed
  source with original size/mtime restored permanently blocks that composition
  from publishing until explicit reprobe/reload and fresh presentation.
  Full-file SHA validation detects tampering without inventing native samples
  or disturbing normal no-hash playback, the 1–3 PCM layers or ONE queue.
  Real Maya/FFmpeg EXR tests cover changed bytes, atomically replaced files,
  symlink alias security, bounded file size, Hold vs source provenance and
  primary GUI→managed Export UI rejection, with author-reload recovery.
- Hardened the *primary* Qt/Maya `Player` presentation lifecycle against
  **reentrant source replacement while decoding, precision extraction or
  seeking**. Source identity, media generation, current requested frame and
  a monotonic request serial now fence each result after a potentially
  reentrant `get()`, `get_payload()`, cached pixmap or FramePump callback;
  a previously selected frame is never published over a newer source.
  Source replacement correctly resets `frameChanged` at session frame 0.
  Sparse Hold native origins use logarithmic existing-label lookup and closed
  SequenceSource instances cannot restart decoding. Real Maya2024 FFmpeg
  EXR/Qt tests exercise changed source colors/signed labels, signals emitted
  mid-decode, rapid queued seeks, sparse policies, actual corrupted files,
  cancellation and deterministic stop/shutdown. No separate playback clock,
  second queue, media rewrite or automatic Session recovery save was added.
- Completed a connected **equal-PAR/different-native-extent comparison**
  delivery increment. A/B/Flicker now opt into source-specific, full-reference
  CPU Working float field rectification whenever the first two plates differ
  in native width, height **or** PAR. Same-PAR 16×8 vs 8×8 and 16×8 vs 16×4
  keep physical centering/transparent outside pixels; taller or wider native
  members are physically cropped to the first canvas, not stretched. Real
  signed FLOAT EXR samples (negative/>1 HDR and .5/.75 straight alpha) remain
  original under read-only visible-member pixel inspection even after cache
  eviction/Hold/selection changes; identical geometry and SEQUENCE retain
  original native paths. All image-only differing-size compositions now freeze
  **v2** per-source geometry even when PAR=1 rather than silently sending
  incompatible v1 stretched output. Actual Maya2024 preview→native inspection
  →A/B Session save/reopen→Export UI→ONE durable queue→independently decoded
  Working float EXR and Display PNG16 verified original signed frame −2,
  24×8 selected physical pixel centers/alpha and linear .25→sRGB .537 blue.
  Quarter-turn rotation, flip and native crop keep the QGraphicsView affine
  invertible, with correct original cached Source values; managed composite
  output still deliberately fails closed for unbaked rotation/crop/flips.
  GPU HDR, arbitrary EXR deep/offset frames and interactive workstation
  render/OCIO are NOT certified by these real Maya offscreen tests.
- Made unequal-PAR **A/B and Flicker** review genuinely reference-field
  accurate instead of displaying unrectified selected plates. Native selected
  FLOAT/HALF/UINT cached precision is Source→Working converted once and
  rasterized on a bounded first-source physical square-pixel canvas, preserving
  negative and over-one HDR, fractional alpha, transparent outside coverage,
  source-specific color and signed original sample identity. Native inspection
  inversely maps the actual visible selected image pixel into the original
  cached source without a new seek; a missing native payload or Qt-only fallback
  cannot claim physical parity. Equal-PAR A/B/SEQUENCE still return their
  original selected Source payloads. Warm Grid→A/B/Flicker switches now restore
  the reference PAR while keeping the original Session review and audio state.
  The original managed v2 physical Working EXR/Display PNG queue may now accept
  unequal PAR **only** after the current source/frame/QPixmap was genuinely
  presented in full-resolution Working float reference-field form; stale,
  downsampled and unsupported presentations still fail closed. Real Maya GUI
  compared primary A/B Working float, visible original native source pixel
  and independently decoded one-queue Working EXR (24×8 for 16×8 native
  3/2 vs 1/2 PAR) at real selected-source edges and centres. Managed v2
  Flicker now also freezes the exact integer `flicker_frames` period (1–120)
  and selects A or B from each output's **absolute session-frame index**, not
  from its encoder position or one permanent active index. Real Maya GUI
  frame0=A/frame1=B 24×8 Working float images independently matched both
  delivered EXR files, including native signed labels and fractional alpha;
  malformed periods, forced A/B overrides, legacy v1 or mixed-movie v3
  Flicker fail strict export validation. Also closed a native-geometry escape:
  equal member PAR alone cannot authorize selected output if raster dimensions
  differ; v1/v2 nonidentical selected geometry now requires the identical
  strong presented-precision proof or rejects. GRID consistently uses square
  canvas pixels even when both members have the same non-square source PAR;
  real Maya tests prove both lifecycle transitions. No GPU monitor, physical audio,
  cross-platform or interactive workstation certification is implied.
- Full corrected Maya2024 QApplication regression **1,185 tests OK (3 skipped;
  436.997 s)**; host Python **1,075 tests OK (500 expected Qt/NumPy skips)**;
  Python3.9 AST **208 source files**, compileall and `git diff --check`
  all passed. These supersede the previous round's 1,177/1,067/207 results
  recorded below. No interactive Maya2023, real GPU HDR/OCIO workstation,
  physical speaker or authenticated ShotGrid release gate was certified.
- Advanced the actual Maya CPU precision comparison and delivered geometry
  acceptance gate. Pixel-center-mapped Source→Working float compositor now
  preserves signed/overrange EXR HDR, fractional/straight alpha and correct
  unequal-PAR physical image coverage; GRID samples original source data into
  each tile only once and does not duplicate aliasing via a full-canvas resize.
  The existing 8-bit fallback retains its truthful limitations. New mode-aware
  `CompositeSource.inspect_member_pixels(...,mode='visible')` binds the current
  displayed pixel to actual Wipe/Checker/Split/Grid, selected A/B/Flicker, or
  dual-operand Blend/Difference/Over native sources with stable source IDs,
  signed frame identities, original HALF/FLOAT/UINT channels, EXR canvas
  padding and explicit absence of synthetic source precision. Primary Maya
  hover and pinned readouts wait for a source/frame/QPixmap presentation receipt
  rather than showing newly sought source values over old preview pixels.
  Switching directly to a single plate clears a stale comparison even if that
  plate previously belonged to the old grid/sequence, preventing wrong later
  export choices. This earlier increment safely rejected unrectified
  unequal-PAR A/B/Flicker; the selected physical-field path documented above
  supersedes that specific preview/output restriction. Actual Maya2024 GUI
  source switching/Wipe/Checker/Grid and precise unequal-PAR EXR Blend through
  the ONE durable queue independently proved native Working float EXR versus
  sRGB 16-bit Display PNG values and both alpha/frame maps; no GPU/HDR monitor
  or workstation OCIO certification asserted.
- Added a primary Maya **Native-aware Pixel Magnifier** in View → Image
  Inspection. A read-only mouse-transparent floating viewport panel displays
  an exact nearest-neighbor 9×9 current Display-pixel grid without drawing
  annotation/crop/Grid artifacts into the image. Original source HALF/FLOAT/
  UINT storage RGBA, signed original frame, precise stage and EXR window/padding
  are reported separately, never synthesized from 8-bit Display samples.
  Native zoom/PAR/quarter-turn/flip, pinned A/B points, source replacement,
  late Qt callbacks and error frames use current generation-safe inspector
  coordinates. Real Maya 2024 widget/Qt geometry and native HDR probes passed;
  hardware GPU/HDR/OCIO monitor parity remains unverified.
- Consolidated intentional **Black/Checker/Error missing-frame rasterization**
  into one Qt-free bounded packed RGBA8 implementation used by actual Maya
  SequenceSource preview and the managed Display PNG16 writer. Black remains
  opaque black, Checker uses identical original cell geometry/palette and Error
  uses a font-independent red-band/diagonal-X error slate. Signed sparse
  source labels and decode_error vs intentional_hole remain separate from
  synthetic image content; native source HDR payloads cannot arise from a
  placeholder. Real Maya 2024 offscreen import/seek→Export UI→single durable
  queue→independently FFmpeg-decoded Display PNG16 confirms per-pixel exact
  16-bit channel conversion and immutable signed frame_map; worker tests also
  verify leading/trailing stepped holes, RGB/RGBA, corrupted real inputs and
  rollback preserving preexisting outputs. Direct pixel parity is bounded to
  native unscaled single-source Display PNG16 holes, not transformed composites
  or color-managed Working EXR, and no GPU monitor certification is implied.
- Extended the opt-in one-layer v4 record PCM overlay to an additional **bounded
  ordered set of one to three** independently authored external WAV occurrences.
  The new Maya Export **Manage ordered audio-only layers…** table exposes each
  source path/in-point, record offset, duration and mute/half/unity gain,
  supports explicit reordering/removal and makes saturation order visible.
  Existing v1 Session, singleton overlay and receipt semantics stay compatible;
  v2 saved intent contains an exact edit fingerprint and a detached ordered
  event list, never a media-proof shortcut. The SAME authoritative rational
  `24000/1001`/48k clock gives exactly 2,002 samples/frame for each independently
  frozen occurrence. Stereo PCM16 contributions are truncated toward zero and
  saturated after every event, so overlapping beds and record gaps are not
  silently normalized. Source SHA, stream ordinal and decoded PCM are rechecked
  before/after each mix; same final buffer feeds Qt audition, per-frame labeled
  waveform, one staged WAV and optional MOV/NUT. EDL/OTIO warns that the new
  ordered audio-only tracks cannot be round-tripped and refuses stale intent.
  No crossfade, arbitrary DSP, sample-unaligned offset or physical device claim.
- Added a bounded **one-layer editorial audio-only overlap** in v4 record
  SEQUENCE/EDL/OTIO: artists explicitly select an independently proven
  canonical zero-origin stereo PCM WAV, unique occurrence identity, record
  offset, original audio in-frame, length and exact 0/half/unity gain. One
  authoritative `24000/1001` record clock yields 2,002 PCM samples/frame;
  independently sampled overlay and original per-cut soundtrack use precisely
  defined signed truncation and saturated PCM16 addition, even over explicit
  picture-only gaps. Exactly the same mixed source-proof bytes feed record
  waveform, video-master Qt audio audition, durable WAV, optional MOV and NUT.
  The new Maya **Audio-only record layer…** dialog changes no Session unless
  the artist explicitly selects Remember, then freezes an exact editorial
  fingerprint rather than pretending Session metadata proves source audio.
  Changes to the edit list invalidate old intent; EDL/OTIO require informed
  lossiness consent for the unsupported overlapping audio-only track. No
  unproven effects, independent arbitrary sample delays or speaker-latency
  claims. Real Maya GUI→queue tests independently compared every signed PCM
  sample including the formerly silent authored picture gap.
- Added a complementary **opt-in lossless RGBA16/PCM record NUT** to the
  existing v4 Display PNG16/WAV queue. The primary Maya Export UI freezes
  original source SHA256 and explicitly warns that FFV1 16-bit straight alpha
  is **Display SDR**, not source-linear float HDR. The same private durable
  sequence folder may also contain optional 8-bit SDR MOV; both derivative
  outputs are independently redecoded, verified and included in the queue's
  snapshot/receipt/recovery signature before atomic publication. Real FFV1
  `gbrap16le` preserves every 16-bit RGBA channel, fractional and transparent
  alpha, and uncompressed stereo PCM; exact NTSC frame/video/audio clock is
  preserved via NUT `1/48000` time bases and video PTS `0,2002,4004,…`
  rather than Matroska's observed millisecond rounding. Broad third-party NUT
  playback is not certified. Maya UI tests exercised simultaneously selected
  MOV+NUT from one queue, exact decoded stream PTS and PCM, and the authored
  overlay mixed over an otherwise silent picture gap.
- Hardened Maya scene restore against deferred Qt timer ordering under heavy
  FFmpeg/Qt integration load: the final source/playhead correction now starts
  a short guarded quiet period *after its actual completion*, rather than
  racing an independent absolute 250ms callback. Generation fencing still
  prevents an older restore or late decoder from clearing a newer review's
  guard. The formerly failing restore-close/newer-restore regression now passes
  three successive complete focused Maya Qt runs; no Session autosave was added.
- Added a bounded opt-in **Maya record PCM audition** on the actual Qt5 audio
  output path, after independent selected-source proof. The existing visual
  `FramePump` remains the only record playhead; a cancellable background worker
  freezes/redecodes the precise 48 kHz stereo s16 source samples, including
  repeated occurrence cuts and exact 2,002-sample record-frame gaps, then
  Qt5 QAudioOutput consumes a bounded seekable PCM buffer. Pause, seek,
  scrub, direction/loop changes, volume, mute and reload use generation-safe
  video-master calls; inaccessible devices and unsupported speed/offset/retime
  refuse without inventing a second sound clock or declaring device accuracy.
  The Maya Export UI makes audition an explicit user action separate from
  sample-verified waveform inspection; diagnostics truthfully distinguish
  verified PCM samples from *uncalibrated physical speaker latency*.
- Added optional **same-queue v4 SDR record MOV** alongside the original
  Display PNG16/WAV reference folder. Artist-confirmed Output intent freezes
  a strict lossless H.264 RGB 8-bit black-matted derivative and uncompressed
  PCM 48 kHz in `record_av.mov` inside the SAME private-stage/atomically
  published directory. It is NOT HDR/alpha-preserving or a standalone movie.
  Each decoded movie pixel must equal independently decoded Display PNG16
  after explicitly defined black-matte/8-bit conversion; decoded movie PCM,
  video PTS `24000/1001`, audio sample count, stream identities, source SHA,
  receipts and recovery signatures are reverified. Unsupported stage, large
  footage, colored HDR output, unproved mix/retime/offset or forged MOV/receipt
  rejects without compromising the existing v4 PNG/WAV delivery and backup.
- Guarded EDL and OTIO interchange against silent loss of the independently
  selected per-edit soundtrack routes stored in Session. A valid selected
  record-audio intent produces an explicit lossy-export notice and prior
  consent; a stale/corrupted occurrence fingerprint now fails export instead
  of reassigning sound to another clip. Current CMX/OTIO exports do not claim
  to roundtrip external audio paths, embedded stream ordinals or verified WAV.
- Integrated record-timeline *viewer transport, explicit per-occurrence Session
  audio intent and frame-exact PCM waveform inspection*. A pure, immutable
  record-frame address in `Composition` and native `CompositeSource` separates
  record indices, repeated occurrence positions, verified movie decoder indices
  and negative/sparse original sequence labels. Fixed sparse PNG/TIFF identity,
  out-of-range record-frame clamping and stale decoder callbacks after shutdown;
  record gaps, holds, source holes and decoder failures remain distinguishable.
  The actual Maya **Verify record waveform…** action constructs a frozen v4
  record plan and requires explicit artist confirmation before a generation-safe
  Qt worker redecodes the original selected embedded/external PCM. It renders
  exact 2,002-sample/frame, signed stereo peaks along the shared record clock;
  a new exact-frame timeline paint mode does not smooth sound across intentional
  silent record gaps. Read-only waveform navigation and export intent never
  autosave. Optional explicit **Remember per-edit sound choices** persists typed
  Session intent through Save/reopen with strict edit fingerprint validation;
  each new export or waveform still independently re-proves original audio.
  Source-owned playback sound is unchanged, physical multi-cut audition remains
  unverified, and viewer opaque-black record gaps are not misreported as the
  transparent-black image sequence export representation.
- Extended opt-in v4 record PCM delivery with a **separately verified external
  zero-origin WAV per editorial occurrence**, including repeated physical
  Sources with different selected external or embedded sound. The external
  proof freezes canonical stereo 48 kHz s16 PCM WAV contents, exact sample
  length, start/stream identity and SHA256; selected source-local movie decode
  or image frame indices map to rational `24000/1001` cut boundaries. External
  samples and published WAV are independently decoded and compared at the
  original durable queue's staging, commit and recovery gates. The Maya Export
  panel now supports export-only **Audio per edit…** choices without changing
  Source playback routes or triggering unchanged-session autosaves. Unsupported
  offset, symlink, arbitrary WAV chunks, compression, overlaps, speed and
  soundtrack mux continue to reject; this is not real-time per-cut speaker
  switching or physical device synchronization.
- Added bounded, opt-in v4 **record-timeline PCM WAV delivery** through Maya
  Export and the original single durable image-sequence queue. Explicit
  per-occurrence verified embedded 48 kHz stereo PCM s16 selection, repeated
  logical Source/cut IDs, exact `24000/1001` frame-to-sample boundaries,
  actual source decode indices and intentional gap/image-edit silence produce
  one `record_audio.wav` inside the atomically staged/promoted EXR/PNG output
  folder. WAV bytes are compared with independently decoded original source
  sample sections; SHA, source provenance, receipt and output directory
  signature detect tampering. Simulated interruption/recovery and reversible
  overwrite preserve existing artist folders on failure. Unverified external audio,
  codec conversions, independent delays, record AAC/MP4, crossfades,
  non-1:1 retimes and uncertain movie starts explicitly reject. Real Maya UI
  → queue → FFmpeg → 5 decoded EXRs and 10,010 PCM samples, including a 2,002-
  sample silent gap, is regression-tested; Qt5 physical speaker timing is not.
- Added independent **precision native EXR stabilization renderer** with
  frozen SHA256 of signed original source-frame sequence, tracking/transform,
  native channel/window geometry and explicit linear color interpretation.
  Real FFmpeg HALF/FLOAT canonical EXR input is resampled in float32 to RGBA
  Working EXR with bilinear scene-linear translation and transparent, zero-RGBA
  unsupported edges. Renderer rejects weak/lost/manual/ambiguous tracks,
  noncanonical EXR windows, non-square PAR, rotation, changed inputs and
  unknown color; no Display/OCIO/HDR monitor parity is claimed. Its opt-in
  Maya Export UI enforces exact viewport plan and a limited original frame
  range; publication is gated on a matching durable queue receipt.
- Rebased **EDL/OTIO absolute editorial source timecode** after verified
  time-alignment to decoder indices: a 24000/1001 cut at 01:00:10:00 no longer
  reexports as 01:00:20:00. Preservation is proven per edit occurrence,
  respecting repeated physical reels, logical Source IDs, rational rate, trim
  and record gaps; unverified rebase demands explicit lossy interchange consent.
  Imported audio-only CMX edits and inconsistent OTIO vendor record proofs now
  block verified v4 media delivery rather than silently dropping audio or
  approximating a transition. Embedded movie TC, signed sparse source labels,
  strict FPS and file-not-truncated UI export have new FFmpeg/Maya tests. A real
  installed studio OTIO adapter remains unverified.
- Added native Maya **Stabilize View (preview only)** backed by immutable
  source-owned translation transforms for signed sparse, quality-proven tracks.
  Applies camera pan to the entire Qt scene (plate, crop, annotation overlays,
  native pixel probe) without rewriting source pixels; source-specific enabled
  preference/anchor persists dirty-only, and tracked frame seeks do not
  autosave. Nonrectangular/perspective, lost/held/manual, weak or unmatched
  frames reject; unverified stabilized media delivery refuses before queue.
  Full Maya viewport geometry and real FFmpeg/OpenCV optical-flow integration
  are covered, but hardware HDR and export re-sampling remain out of scope.
- Selected audio waveform now reflects the chosen embedded ordinal or attached
  external file on video time with positive/negative delay. Fixed false near-
  zero envelopes from decoding high-frequency audio directly at the 1 kHz
  waveform rate: rectify before downsampling; per-stream/per-delay cache keys,
  bounded request validation and QProcess cancel/reuse remain intact. Real
  FFmpeg-generated two-track PCM and Maya UI tests exercise switching and
  waveform time offsets; mono peaks are visualization, not Qt speaker routing.
- Introduced `tests/run_maya_suite.py`: creates a single `QApplication`
  **before** loading/discovering any Qt image tests, enables early Qt context
  sharing attribute, and converts unexpected modal warnings/critical dialogs
  into test failures. This fixes the previously reproduced bootstrap abort
  (`QPixmap::fromImage` before `QGuiApplication`, then `QWidget` abort) without
  modifying application/runtime Maya behavior. Offscreen QOpenGL warnings do
  not certify GL context or HDR hardware.
- Connected tracking to portable `Source.review` with original signed frame
  identities, exact rational FPS/geometry, native flip state and bounded
  source-specific centers/quads/real optical-flow quality. The Maya panel now
  shows confidence (heuristic, not probability), feature/inlier/error counts,
  explicit loss/held status, source-key-safe relink refusal, and translation-
  only **Correct Current Frame** with one-key manual repair and undo/redo.
  A known motion loss blocks delivery before Export Options/queue; no unknown
  motion is silently delivered as a verified tracked transform. Dirty-only
  persistence means unchanged review/navigation creates no autosave. Real
  FFmpeg MPEG4+B-frame nonzero seek tests and OpenCV regressions verify
  original decoder index and signed source frames.
- Hardened audio route changes: signed delays use the separate existing
  FFmpeg video + Qt audio backend instead of unoffsettable native Qt A/V,
  including native-to-fallback transition without losing frame/range/loop/
  playing intent, coalesced pending seeks and signed offset after loop wrap.
  Explicit Qt track-zero reselection, rational frame-to-millisecond timing and
  read-only **Playback → Audio → Routing & Sync Diagnostics** describe effective
  backend/clock capabilities and honest Qt5 DSP/device limitations. No remote
  audio delivery or studio hardware/speaker timing proof is claimed.
- Protected managed EDL/OTIO delivery against *preview-only* editorial
  approximations. The actual Maya v4 snapshot now reconciles saved original
  EDL/OTIO member trims/timecodes against freshly probed movie/sequence
  origins and persisted interchange warnings, refusing unsupported
  dissolves, overlaps, motion effects and ambiguous source in/out instead of
  silently delivering a different shot. Added pure, session-reopen and Maya
  tests covering rational record gaps, stale relinks and informed refusal.
- Hardened the real OpenCV tracking launch for negative/sparse sequences:
  dense playhead frames resolve through explicit signed on-disk Source IDs,
  rather than indexing the compact filename array. Preflight rejects
  intentional holes and vanished existing plates before creating a tracking
  QThread, with bounded 2,400-frame jobs and original dense behavior retained.
  Added real Maya UI and OpenCV pixel-mapping regressions.
- EDL and OTIO JSON export now share private same-directory atomic staging,
  without predeleting existing files. The Maya exporter requests affirmative
  consent for known lossy transition/track/retime conversions; declining
  leaves the prior artist file intact. Tests cover a simulated failed atomic
  promotion, symlink refusal and both Maya decline/accept outcomes.
- Added opt-in **version-3 mixed-media composite export** through the actual
  Maya Export dialog, user-confirmed per-movie input-color/YUV provenance,
  immutable exact CFR FFprobe/PTS and full-movie SHA256 proof, member-specific
  logical Source IDs, signed sparse EXR Hold semantics, rational decode-index
  mapping and CPU linear-light Working EXR/Display PNG16. Single persistent
  queue stages and validates every output and receipt, preserving source bytes
  and existing output across cancellation, rollback, tamper and recovery.
  Real movie+EXR FFmpeg samples, alpha/HDR channel extremes and the complete
  window → journalled queue → FFmpeg → decoded EXR path are tested. VFR, movie
  alpha, unknown color, rotations/PAR, MP4/mixed audio output still reject.
- Added opt-in **version-4 managed EDL/OTIO record delivery** for 2–8 ordered,
  nonoverlapping hard-cut occurrences over at most 120 record frames, with
  frozen per-cut stable occurrence/source IDs, dense source trims, original
  signed sparse frames, exact rational FPS, verified CFR YUV or canonical EXR,
  color/alpha, and explicit transparent RGBA0 gaps. The same Source may appear
  in multiple edits without losing color or review identity. Reuses the
  existing CPU Working EXR/Display PNG delivery and *same* durable queue,
  including immutable receipt validation, source SHA256, cancellation,
  overwrite rollback, crash recovery and explicit retry. Actual Maya Export
  UI → journalled queue → FFmpeg → five EXR frames validates duplicate-Source
  cuts, differently named occurrences and transparency. Overlaps, arbitrary
  retimes, mixed audio, MP4 delivery and unsupported pixel geometry fail closed.
- Preserved distinct logical review Source identities across selected
  duplicate media paths and EDL/playlist order. Movie, EXR and sequence
  comparisons now freeze distinct input-color and missing-frame policies,
  while native NTSC session-rate rationals survive actual Maya viewer setup.
  Deferred crop restore is also invalidated by an artist's subsequent native
  crop edit even when the same Source remains active; unchanged navigation
  does not create an autosave.
- Added bounded asynchronous real 16-bit CPU SDR movie member payloads for
  supported 9–16-bit SDR sources in mixed EXR/movie viewing, with source-owned
  inspection/scopes and explicit non-HDR labels. Tested signed HDR EXR values
  remain unclipped through shared working-light composition, with 8-bit Qt fallback,
  stale generation cancellation and bounded precision cache/worker lifecycle.
- Wired opt-in unequal-PAR native EXR comparison delivery into the existing
  Maya Export panel and single durable queue. Frozen rational member PAR,
  canonical full EXR window geometry and original per-member source identity
  drive the same working-linear physical-field compositor as the viewer.
  Overlap exports use an exact square-pixel reference raster; Grid retains
  transparent stretch-to-cell. Only float Working EXR and Display PNG16 image
  sequences are permitted. Real FFmpeg/OpenEXR output sampling verifies
  member edges, alpha, sparse signed/Hold labels and square output PAR;
  staged queue receipt, tamper, cancellation and original-file preservation
  are tested. Non-EXR members, offset windows, movies and unverified effects
  reject before delivery; v1 canonical composites remain supported.
- Added saved-session-only local Review attachment authoring through the real
  grey sidebar: safe bounded file/still selection, exclusive portable
  source-owned copies and stable typed IDs, exact optimistic source/revision
  checks, optional removal of unsent references without deleting either the
  original or its retained managed copy. Preserves note/reply drafts, rejects
  stale/locked/synced/conflicted/remote attachments and never publishes them
  to ShotGrid without separate consent. Cross-directory **Save Session As**
  now relocates trusted source-owned managed copies with bounded per-file and
  total sizes, exact SHA256 and size proof, no-symlink directory handles,
  exclusive creation or identical-target reuse, rollback of only new copies
  on error and atomic new JSON only after asset verification. The original
  saved session and artist files remain untouched; normal same-dir saves,
  dirty-only recovery and the three automatic recovery snapshots are unchanged.
- Fenced Maya scene-state delayed restoration with per-window generation and
  logical-source identity checks. Loading other media, timeline scrubbing,
  starting playback or window shutdown cancels stale deferred seeks and Maya
  sync re-enablement. Overlapping Qt restore timers can no longer clear a
  newer restoration's no-autosave guard; focused real Qt timer tests cover
  those races without creating recovery snapshots from navigation.
- Reference/Playblast A/B swaps now retain a playhead with a generation-fenced
  deferred seek only while the destination media object, logical Source ID
  and navigation state remain unchanged. A later manual scrub, source switch,
  Playback start or closing window cannot trigger an old swap's stale seek.
- Sequencer playblast cache now rechecks Maya's modified-scene flag and saved
  scene mtime at render time, not only during shot inventory. Unsaved, untitled
  and explicitly re-rendered shots use isolated uniquely staged revisions;
  previous reviewed JPEG sequences remain intact. A scene saved since the
  inventory requires fresh discovery; clean same-revision cache remains
  reusable and stable logical source IDs, shot timing and camera restoration
  are preserved. Maya GUI/interactive scene graph freshness remains unverified.
- Replaced first-frame-only Sequencer cache validation with bounded complete
  frame-set integrity manifests. Staged JPEG output records canonical names,
  counts, per-frame sizes and SHA256; reusing cached review material verifies
  all frames and rejects missing/modified middle frames, symlinks, injected
  entries and malformed or legacy unverifiable manifests. Corrupted or old
  cached revisions are never overwritten/deleted; isolated verified new
  playblasts preserve the original camera/time and stable Source IDs.
- **Current integrated regression baseline (after precision inspection and sparse Display parity):**
  Maya 2024 `mayapy` offscreen **1,177 tests OK (3 skipped; 435.647 s)**
  using `tests/run_maya_suite.py`, complete host Python suite **1,067 tests
  OK (492 optional-dependency/Qt skips)**, **207 Python 3.9 grammar-compatible
  files**, `compileall` and `git diff --check` passed. Actual Maya 2023 GUI,
  studio OpenEXR/OCIO and authenticated ShotGrid, GL HDR monitor, physical
  speakers and Windows/Linux/network share behavior remain unverified. All
  changes remain in the live, deliberately dirty Maya package; no Git commit,
  reset, clean or push was performed.
- Added source-owned edit/delete for unsent local typed review replies through
  the actual Review Records UI, with stable IDs, expected-revision conflict
  guards, layer/remote-status protection and preservation of unapplied main
  note edits. Synced/imported replies remain read-only; no remote mutation.
- Optional GPU multi-viewer now reports actual context/texture activation and
  explicit CPU SDR fallback, prepares native float pixels through the shared
  CPU color reference for RGBA32F upload and guards Qt/PySide2 texture and
  driver ABI calls. Still opt-in, never claimed hardware HDR/OCIO parity.
- Frozen per-source EXR displayWindow/PAR is validated for every decoded
  frame; varying cropped dataWindows remain valid. Live unequal-PAR overlap
  comparison viewport now uses the shared first-member physical PAR, while
  stretched Grid intentionally stays a square-pixel montage.
- Connected signed native EXR dataWindow/displayWindow rectification to the
  actual Maya source probe, image-sequence decoder and primary SDR viewer.
  The immutable FramePayload carries real native sample dtype, PAR, absolute
  signed EXR windows and preserved negative/HDR values; missing display
  canvas regions stay transparent without claiming an original HDR sample.
  Primary inspector reports absolute EXR coordinates and distinguishes padded
  canvas pixels. Unsupported FFmpeg cropped EXR views remain explicitly
  unavailable instead of guessing image placement.
- Added EXR Source/Working scope coverage selection between actual native
  dataWindow and full display canvas, with generation-safe asynchronous
  results; original HDR under/over-range is no longer diluted by padding
  unless the artist requests canvas-inclusive scopes.
- Added physical-field CPU precision comparison using a shared source PAR
  geometry model and inverse sample coordinates for member probes. Mismatched
  PAR members retain native linear-light RGB/alpha and transparent outside-
  plate regions in live overlap modes; default square-pixel comparisons,
  image A/B/Flicker identities and transparent full-stretch Grid stay
  unchanged. Managed unequal-PAR comparison exports still reject.
- Connected optional managed single-source PAR, native crop, flip and
  quarter-turn rectification to the actual Export UI and existing persistent
  queue using an explicitly confirmed, immutable native→square-pixel plan.
  Supported Working float EXR or Display PNG sequences validate final raster
  and signed frame identities; legacy Source, movie and composite formats
  continue to reject unverified geometry rather than silently flattening it.
- Added native comparison-member original-pixel hover/pin inspection and
  independent Source/Working member scopes without assigning nonexistent
  original HDR precision to the final SDR composite. Cleared stale scopes
  after missing precision or member changes; preserved original signed frame
  IDs and per-source color interpretation without autosaving hover navigation.
- Hardened native EXR part/layer/window handling and explicitly selected
  RGB(A) view identity across playback, source metadata and recovery. Invalid
  header geometry, ambiguous layers and unsupported FFmpeg raster fallback
  reject instead of silently returning misleading pixels. EXR view relinking
  now uses the same validated source probe as normal media import.
- Added opt-in embedded soundtrack preservation for eligible single-source
  managed CFR Display MP4 exports. A confirmed frozen stream/timebase selects
  one stereo embedded audio track, trims decoded samples against the exact
  fractional-frame clock, encodes 48 kHz AAC and FFprobes final output and
  durable receipts. No external audio/delays/retimes/VFR or unverifiable PTS;
  existing video-only and image-sequence output paths remain unchanged.
  Source identity is checked throughout staging so replacing an input movie
  during decode/mux causes rollback, not an incorrect accepted delivery.
- Added Playback → Audio → Select Embedded Audio Track to the actual Maya
  window, listing bounded-probed embedded tracks by audio ordinal, FFprobe
  stream ID, language and channel layout. The per-Source selection feeds
  verified soundtrack delivery and preserves prior delay/settings; repeat
  selections and cancellation never cause a review autosave. Qt5 audio-device
  playback of a non-default track remains capability-dependent.
- Fixed compatibility with older EXR reader adapters when no layer/part was
  selected, and closed headless Maya UI test callback/shutdown fixture gaps.
  Explicit EXR selection now creates a source-owned dirty change only when
  the selected view actually differs; missing audio timestamps or a timed-out
  FFprobe never imply valid alignment.
- Wired true Source/Working/Display stage selection into the actual scopes
  dialog and Maya window. Source and Working use native float FramePayload
  through generation-cancelled workers, preserving signed HDR/negative values;
  Display uses the QPixmap actually presented. Missing native samples and
  noninvertible working composites fail explicitly. Real FFmpeg EXR samples
  and all three scopes stages now have main-window integration coverage.
- Added one Qt-independent pixel-aspect/quarter-turn geometry model and the
  actual QGraphicsView matrix path. The image, notes, crop, selection, onion
  skin and pinned probes use the same native scene coordinates under PAR,
  quarter-turn rotation, flip and zoom. The View rotation controls persist
  source-owned orientation without redundant recovery writes; non-square/
  rotated output fails closed until the export geometry matches the preview.
- Delivered managed HDR **comparison exports** from real selected image
  sequences. Source-specific input color/alpha and signed sparse/held lineage,
  rational FPS and mode settings are frozen in the same serialized export
  queue. Two-source A/B, Blend, Wipe, Split, Checker, Over, Difference,
  Inverted Difference and multi-source Grid use the CPU float working-space
  compositor already used by playback. Real FFmpeg Working float EXR,
  Display PNG16 and supported opaque MP4 are validated against CPU reference
  pixels and frame manifests. The queue verifies the entire member map,
  rollback and cancellation; audio omission requires user confirmation.
- Added pinned A/B float HDR pixel probes to the actual PlaybackSurface,
  with image-space markers that track zoom/flip/playback, explicit source vs
  working vs display sample deltas and no stale HDR values after decode errors.
  Menu actions are under View → Image Inspection. Probe navigation does not
  dirty notes/sessions or trigger review recovery.
- Hardened audio-master/frame transport: Qt soundtrack stalls switch to the
  existing bounded video clock, recovering audio cannot rewind late video,
  terminal soundtrack events cannot restart audio via stale frame notices,
  and seek acknowledgement suppresses repeated decoder flush/corrections.
  Qt fake-device stress tests cover stalls, repeated EOF, shutdown and seeks;
  actual speaker/driver behavior still requires interactive Maya validation.
- Enabled opt-in CPU-managed CFR movie inputs with actual FFmpeg Float RGB(A)
  decode and Source/Working/Display stages, strict frame-index and signed
  source-frame manifests, rational rate checks, user-confirmed input color
  assumption and YUV matrix/range, 16-bit PNG, floating EXR, alpha-aware
  rejection and bounded video-only MP4. Outputs share the durable queue,
  reversible overwrite rollback, managed provenance/recovery checks and
  staged post-verification. Variable frame rates, audio delivery, mixed-movie
  composites/tracking/burn-ins and unverified OCIO/GPU remain unsupported.
- Prior combined regression after the precision scopes, geometry and managed
  composite integration: Maya 2024 `mayapy` **669 OK (1 skipped)**, host Python
  **666 OK (224 expected dependency/Qt skips)**; **132 Python 3.9 syntax**
  files validated, compileall and git diff --check clean. This includes real
  FFmpeg movie/EXR/composite pixel fixtures, durable export recovery and Qt
  audio/inspector geometry tests. Live Maya, speaker, GPU and studio services
  remain unverified.
- Reconciled the entire 17-section OpenRV improvement roadmap against the
  live source and test tree; recorded implemented vs partial features and
  unverified workstation/studio release gates in `OPENRV_IMPROVEMENTS.md`.
- Integrated signed sparse missing-frame choices across probe, Player,
  Sequence/Grid composite members, Playback menu, Source.review, session
  restoration and notes. Legacy Skip remains the default. Hole diagnostics are
  distinct from actual corrupt media, and spans are bounded.
- The primary Qt viewer now renders its SDR pixmap from source-owned float EXR
  pixels through the declared input/working/display transform before 8-bit
  encoding, with optional OCIO, a native precision inspector and visible
  fallback status. Color Pipeline settings are editable per source; real GPU
  HDR framebuffer and studio OCIO monitor parity remain unverified.
- Wired Working/Display color output to the existing durable export queue, with
  real FFmpeg float32 EXR and display MP4/16-bit PNG tests, signed sparse frame
  mapping, stage-specific dialogs, manifest evidence and atomic publication;
  source stage remains unmanaged and unsupported combinations fail safely.
- Explicit authored image-sequence patterns retain leading/trailing sparse
  frames and reject out-of-range neighbor images, even after session reopening.
  Massive authored ranges are rejected before allocation. Real corrupt-file
  decoder failures now present a distinct slate instead of stale pixels or
  false missing-frame placeholders, without dropping review annotations.
- Added real FFmpeg/FFprobe FLOAT32 RGB(A) EXR decoding fallback when Python
  OpenEXR is unavailable in Maya. HDR highlights, negative samples and alpha
  now reach the actual Player/primary viewer; the fallback clearly reports
  unsupported native EXR windows/layers and stays distinct in capabilities.
- Added precision-aware comparison in shared working light with distinct
  input color settings per source, stable signed per-member lineage, HDR/alpha
  before display conversion and a bounded software fallback. Grid transparency,
  comparison controls, rational time and source identities are preserved.
- Working/Display managed image exports now handle RGBA float EXR and 16-bit
  straight-alpha PNG, intentional Checker/Error slates, signed manifests and
  explicit rejection of alpha-to-H264/JPEG delivery and corrupt existing media.
- The durable export queue now retains a reversible private backup while a
  movie/sequence overwrite is promoted, verifies the promoted output before
  declaring durable success and restores an existing target after a failed
  verification or interrupted commit. Protected symlink/backup paths and
  stalled QThread shutdown are covered by focused recovery tests.
- Added user-mediated offline ShotGrid conflict inspection and recovery: a
  read-only scoped Version/Note check, explicit consent to acknowledge, requeue,
  rebase status or discard local work, repeat read to reject concurrent remote
  changes, and no remote write until the separate confirmed Sync operation.
- Fenced Maya timeline synchronization at shutdown/reload entry and deferred
  flush, so a late frameChanged signal cannot restart Maya currentTime write or
  read timers during teardown; tested with real Maya Qt signals/QTimers and
  synthetic reentrant currentTime callbacks.
- Moved Properties chrome closer to Maya's native Qt grey/selection palette,
  and reorganized File, Edit, Playback, View, Tools and Help into task-oriented
  native menus while preserving registered shortcuts. Flattened common Edit
  operations and J/K/L shuttle, removed the nested speed flyout, moved Playlist
  access to View, and split reference comparison from Maya-only integration.
- Wired canonical typed `ReviewDocument` records through the actual Draw/Review
  sidebar, selected surface object IDs, source-frame mapping, editable comment/
  category/priority/status/layer, lock/hide, undo snapshots, onion skin, JSON
  note open/merge/export, session restore and the existing drawing tools.
  Legacy v2 readers still receive flattened drawings while rich records,
  replies and unknown fields remain authoritative in the extension.
- Added review discussion authoring in the Review Records editor: local replies
  are persisted through the canonical session/JSON model with stable IDs, while
  empty submissions are ignored, locked layers retain unsent drafts, and
  unrelated unapplied edits are preserved. Remote publication still requires
  separate explicit consent.
- Added optional sparse-sequence hole presentation at the SequenceSource layer:
  Hold/Black/Checker/Error, signed frame identities, separate corrupt-frame
  reporting and policy-aware cache invalidation. Default Skip playback remains
  dense; selecting a policy now opts into the declared source timeline.
- Avoided rearming Maya review recovery on unchanged canonical note capture;
  only actual Session.review changes schedule a recovery write, including when
  source switching or refreshing the Review panel triggers a capture.
- Added a persistent sequential export queue to Export Properties. Immutable
  source-stage specs, source identity and notes/transform manifests are
  journaled atomically. Output is staged/validated before commit; users can
  cancel, retry and reveal, and crashed work is recoverable without silently
  restarting or destroying existing output. Real FFmpeg staging/verification
  has automated tests. Composite and unrestricted managed-color outputs still
  require work; the supported image-sequence managed stages are verified above.
- Added a durable authenticated Flow/ShotGrid review outbox to the existing
  version browser: explicit note/status enqueue and separate publication
  consent, background network workers, bounded retry/idempotency, Version-to-
  logical-Source identity, conflict protection and safe dirty note acknowledgments
  only for matching published content. Live studio permissions remain untested.
- Hardened the Flow browser against queued stale query/publish callbacks,
  wrong-Version/Source notes refresh and reentrant disconnect or double-sync;
  added Qt fake-client tests for stale result rejection, publish consent and
  shutdown. Durable completed publishes still require reconciliation on
  reconnect if a remote call finished after local disconnect.
- Extended EXR sequence sources with precision-preserving owned frame payloads,
  color stage declaration and payload-aware cache; source HDR/negative pixel
  values can be inspected in the main surface independently of the SDR preview.
  That primary SDR preview is now transformed from precision pixels before
  quantization. Actual GL + OCIO workstation parity remains open.
- Added live Maya Qt tests for record editing and session/JSON round trips,
  native menu organization, export queue lifecycle, SG consent/identity and
  source-HDR hover sampling, plus Qt-free queue/outbox and media tests.

- Reworked the docked Properties workspace with an explicit source/review/
  framing/delivery tool hierarchy, a consistent scoped dark inspector palette,
  clearer tool titles and descriptions, contextual shortcuts, modern control
  states, restrained card hierarchy and compact styled scrollbars.
- Reordered the Draw workflow to put annotation keys ahead of brush dynamics;
  surfaced Image Scopes and Color Correction as direct Review actions, Import
  Media above read-only source details, and Export Media above output details.
  Advanced collapsible sections remember their open state without triggering
  scene autosaves or changing annotation content.
- Redesigned Playlist as a resize-aware media/compare workspace. Source rows
  use a lightweight Qt delegate with readable active/offline state, version,
  rate and tags; source search and status filtering preserve the underlying
  timeline and selection. Removed nested playlist scroll areas and added
  Maya Qt regressions for compact widths, reorder and tool navigation.
- Corrected Grid tile rendering across NumPy/Qt software paths and GPU
  transforms: every image fills its tile, alpha is preserved, and unused tiles
  remain transparent rather than picking up a black background.
- Stabilized fallback audio transport under FFmpeg cache buffering. REALTIME
  playback keeps its audio-master clock running when video is momentarily
  unavailable; Play All Frames remains video-master. Coalesced seek requests,
  rate-limited large drift corrections and accurate short-audio end handling
  reduce Qt decoder flushing and premature loop restarts. Added synthetic
  audio-clock/seek/lifecycle regressions; studio speaker-device performance
  remains to be tested in an interactive Maya session.
- Added a lossless, Qt-independent rich-review-to-PlaybackSurface projection
  bridge, with owner IDs, editable layers, stale-commit rejection, explicit
  blank drawing keys and preservation of independent records at one frame.
  The bridge is an integration API; the legacy window drawing store is not yet
  replaced by the full review-record UI.
- Fixed imported EDL/OTIO preview to activate the record-time composition
  immediately, preserving record gaps, trimmed single edits and separate edit
  identities when more than one event references the same media path. Session
  reopen restores the same edit rather than previewing just its first clip;
  offline EDL reels resume their record composition once the missing media
  is relinked.
- Scopes now follow actual decoder frame presentation instead of playhead
  notifications; seeks waiting on media cannot rescope stale frames.
- New session imports discard the previous session's saved file association,
  unknown-field envelope and recovery checkpoint; they remain unsaved until an
  explicit session save.
- Added mayapy window regressions for timeline import/reopen, repeated media
  edits, scope presentation sequencing and Qt-free session replacement.

- Began studio-grade OpenRV roadmap integration around one live SessionController:
  stable source/representation identity, portable `.frsession` Open/Save/Save As,
  atomic autosave/recovery, Maya-scene snapshots, and per-source frame-accurate
  drawings, markers, range, flip, and crop restoration.
- Added source-aware Playlist rows, reorder/relink/activation, missing-media
  indicators, and a shared resolver for movie, sequence, folder, notes and
  session inputs. Cross-platform mount mapping preserves authored paths.
- Added A/B switch, flicker, blend, wipe, split, checker, and over comparisons,
  pair controls, mixed-rate source mapping, shared software reference, and live
  composition parameter changes without rebuilding member decoders.
- Added signed J/K/L shuttle, Once/Loop/Ping-Pong transport, reverse-aware cache
  candidate scheduling, bounded cache diagnostics, and reverse-audio muting.
- Added immutable owned FramePayload/geometry, CPU reference color state with
  optional OCIO, HDR-aware compositing and GL shader foundation, stage-aware
  pixel sampling/histograms, plus a display-pixel hover inspector.
- Added immutable source-stage export specification, output probing, atomic
  transform manifest, and burn-in fallback for FFmpeg without drawtext. Color
  stages that cannot yet be exported faithfully raise explicit errors.
- Added redacted Capabilities/Support Report and metadata probing for PAR,
  bit depth, pixel format, codec, color tags, alpha mode, and timecode.
- Added pure and mayapy lifecycle tests covering live session restoration,
  source identity, compositor/transport, color/sampling, export verification,
  hovered pixels under flip/zoom, and support redaction.
- Added zoomable and pannable timeline with visible range, Fit Session/Marks,
  viewport-relative waveform/cache/annotation drawing, and bounded ticks for
  million-frame sessions.
- Added generation-cancelled, asynchronously computed image scopes: RGB/Y
  histogram, luminance waveform, Rec.709 vectorscope, and HDR range diagnostics.
- Added optional OpenTimelineIO and CMX3600 EDL import/export with source/record
  timecodes, trims, rational rates, stable clip identities, gaps, and explicit
  reports of unsupported interchange features.
- Added a record-aware Sequence playhead for imported edits, handling retimes,
  holds/freezes, repeated media, record gaps and overlaps, and a source-TC
  alignment adapter that warns when media lacks an unambiguous timecode origin.
- Removed plaintext Flow/ShotGrid API key persistence and any previously saved
  secret; added active Toolkit user and studio environment authentication.

- Overhauled Playlist into a live playback/composition list: Sequence is the
  default with no Off mode or Compose button; selected rows participate, while
  an empty selection uses every row. Clicking empty list space clears selection.
- The loaded file is always retained and marked as the playlist's active item
  without selecting it, so active playback and composition membership are
  independent. Clear/Remove cannot discard that current default item.
- Grid, Difference, and Inverted Difference now drive a real multi-source
  compositor for movies as well as image sequences. Sequence playback also
  concatenates mixed movie/sequence members through the same source adapter.
- Fixed a launch crash (`KeyError: 'playlist'`): the Tools-menu shortcut map had
  no entry for the new playlist tool. The lookup is now tolerant of tools
  without a shortcut.
- Fixed a `RuntimeError: Internal C++ object already deleted` from the waveform
  and thumbnail workers: a cancelled job's killed `QProcess` could fire its
  queued `finished`/`errorOccurred` signal after `deleteLater`. `cancel` now
  always disconnects those signals before deleting, and the slots are defensive.
- Sequence-mode playlist composition concatenates clips into one continuous
  source and timeline with alternating per-clip colors and Up/Down navigation.
- Added the GPU multi-source viewer (`viewer/gpu_viewer.py`): a `QOpenGLWidget`
  that uploads each frame to a texture once and composites single/grid/
  difference/inverted-difference/over in shaders (ported from OpenRV's
  `Difference`/`Over` GLSL), with a deterministic software fallback via the new
  NumPy `viewer/compositor.py`. The compositor is also the standalone software
  path and is fully unit tested; GL rendering needs a real Maya GL context.
- Added headless lifecycle tests (`tests/test_lifecycle.py`, run under
  `mayapy`) that construct the real window, switch every workspace tool (which
  would have caught the playlist `KeyError`), exercise sequence concatenation
  and clip focus, and regression-test the worker process teardown. They SKIP
  when no Qt binding is present, so the plain-Python suite is unaffected.

- Added `media/sequence.py`: an OpenRV-faithful, Qt-free image-sequence and
  time model. It recognizes `@`, `#`, Maya tick (`` ` ``) and printf `%0Nd`
  frame cookies with offset/multiply/modulo modifiers, parses and emits
  Shake-style frame ranges (`1-10x2`, `1,10,20-30x2`, `-15--10`), and discovers
  a sequence from any member file into a sparse frame-number map that preserves
  holes and negative/non-zero frames. Exposes true first/last, missing frames,
  padding, and pattern, plus a missing-frame policy (hold/black/checker/error).
- Added `FrameMapping`, which keeps the display index, source frame number,
  rational FPS, and (non-drop) timecode as separate domains so negative and
  non-zero sequence frames and 23.976/29.97/59.94 rates do not round or collapse
  onto a zero-based index.
- `utils.sequence_for` now delegates to the new discovery (holes and negative
  frames survive; files are returned in true frame order), and `utils`
  gained `sequence_info`. `MediaInfo` now carries `frame_numbers`,
  `start_frame`, `fps_rational`, and `frame_mapping()`; probing an image
  sequence records the real frame numbers and a movie records its rational FPS.
- Added `tests/test_sequence.py` covering frame expressions, range round-trips,
  sequence discovery with holes/negatives/padding, pattern expansion, rational
  FPS, and the display/source/time mapping.
- Added a burn-in template/token model (`exporting/burnin.py`): `{shot}`,
  `{version}`, `{artist}`, `{filename}`, `{date}`, `{user}`, `{fps}`,
  `{display_frame}`, `{source_frame}`, `{timecode}`, and `{watermark}` fields
  placed in the frame corners/edges, resolved per frame from a `FrameMapping`
  (so timecode and real source frame come from the time model), with built-in
  presets (review/client/minimal/source). Unknown tokens are left intact.
  Qt-free and unit tested; the renderer still draws the resolved text.
- Made the `exporting` package import lazily so its Qt-free submodules import
  without a Qt binding.
- Moved the Playlist from a floating dialog into its own Properties sidebar
  tool (rail button, `P`), as an embeddable `PlaylistPanel`. The panel adds
  multi-clip composition controls.
- Added a multi-clip composition model (`playback/composition.py`) with four
  modes: Sequence (clips play back to back on one concatenated timeline, with
  alternating per-clip colors in the timeline and Up/Down to focus the next/
  previous clip), Grid (tiled, centered), Difference, and Inverted difference.
  The timeline gained `set_segments` to tint clip spans, and Up/Down focus the
  adjacent clip when a sequence composition is active (otherwise unchanged).
  Note: simultaneous decode/render for grid and difference, and cross-clip
  sequence playback, require a multi-source player/GPU viewer that is not yet
  wired; those modes build the composition and report the limitation rather
  than silently showing one clip.
- Added a startup capability report (`core/capabilities.py`) that probes Python,
  the Qt binding, QtMultimedia, FFmpeg/FFprobe, NumPy, OpenCV, OpenEXR,
  OpenColorIO, Flow/ShotGrid, a writable cache, and (best effort) the GPU, and
  reports each with a status, detail, and actionable remedy. Probes are
  defensive (a broken optional dependency never raises) and injectable.
- Added note copy/paste and propagation (`notes/propagation.py`): duplicate a
  note (or a list) onto other frames, across an inclusive frame range, or onto a
  time-aligned source via two frame mappings. Each copy gets a fresh note and
  shape id, records `derived_from`, is marked `copied` provenance, and keeps its
  real `source_frame` offset; author/category/body and unknown fields survive.
- Added ID-based note merge (`notes/merge.py`): a remote or session refresh
  reconciles by stable note id, protecting locally dirty edits (kept, with a
  conflict reported), keeping unsynced local-only notes, and otherwise following
  the incoming set. Timestamp-only differences are not treated as conflicts.
- Added studio/OS path mapping (`core/path_mapping.py`): ordered root-rewrite
  rules applied longest-first at path-segment boundaries, with environment
  variable expansion (`$VAR`/`${VAR}`/`%VAR%`), case-insensitive matching for
  Windows-style paths, a reverse mapper, and an existence-preferring
  `map_existing`. Rules load from a `FRAMEREVIEWER_PATH_MAP` spec. `Source` and
  `Session` gained `remap_paths` so a portable session relinks its media on a
  host with a different mount layout without disturbing review state.
- Added a portable, Qt-free source/session model (`app/session.py`). A `Source`
  has a stable id, timing (start frame, rational FPS, frame numbers), one or
  more representations (playblast/proxy/review/EXR/published) that can be
  relinked or switched without disturbing review state, optional audio, color
  interpretation, metadata, and portable review state. A `Session` owns the
  ordered playlist of sources, the current source, and comparison/color view
  state, and deliberately excludes window geometry and host wiring. Payloads are
  versioned, migrated purely, and preserve unknown fields. `Source.from_media_info`
  bridges a probed `MediaInfo` (including its frame numbers and rational FPS).
- Added SMPTE drop-frame timecode (29.97 and 59.94 fps) to the sequence/time
  model, with a `;` frame separator; non-drop-frame rates are unchanged.
- Added a Qt-free crop/image-window geometry module (`media/geometry.py`):
  separate source aperture, data/display windows, viewer crop, and export crop;
  normalized `CropRect` with pixel conversion, center/reset, overscan, move, and
  aspect-lock; pixel-aspect (anamorphic) display sizing; and named aspect presets.
- Expanded the notes schema to version 2 with pure, forward migration from
  version 1. Notes now carry a stable file/note/shape `id`, a real
  `source_frame` (which may be negative or non-zero) and optional inclusive
  `range` kept separate from the zero-based display `frame`, a coordinate
  `space`, source `id` plus geometry, `author`/`created`/`modified` stamps, a
  `generator` stamp, and `provenance`. Unknown fields are preserved across a
  load/save round trip, and migration does not mutate the input or regenerate
  existing ids. The historical `build_payload`/`parse_payload` behavior is
  unchanged for the viewer; richer `parse_notes`/`build_notes_payload` expose
  the full records. Canonicalization now preserves a shape's `id`.
- Dimmed the timeline thumbnail lane while extraction is in progress (a veil in
  the timeline background color), clearing when the strip finishes.
- Made timeline thumbnail extraction faster. Movie thumbnails now use
  keyframe-accurate fast seeks (`-noaccurate_seek`) and fill a small pool of
  single-frame extractions concurrently (each still `-threads 1` so playback is
  not starved) instead of one blocking seek at a time. Image-sequence
  thumbnails decode at reduced size through `QImageReader` rather than loading
  the full-resolution frame and downscaling, and EXR frames are read through the
  dedicated EXR reader (previously they produced blank thumbnails).
- Added a dedicated Text annotation icon in SVG and PNG formats, matching the
  existing monochrome toolbar icon set.
- Reorganized the formerly flat package into `app`, `core`, `playback`,
  `media`, `viewer`, `notes`, `exporting`, `integrations`, `tools`, and
  `widgets`. Only the public lifecycle entry point and global style remain at
  the package root.
- Note validation and JSON persistence are now Qt-independent, annotation tool
  metadata has one registry, and reusable menu/transport widgets no longer
  live in the main window module.
- Consolidated the small workspace and annotation registries into their public
  package modules, and combined playback timing-policy math with the shared
  cache/playback policy module to avoid unnecessary one-purpose files.
- Replaced the modal text-note dialog with direct on-image typing. Text notes
  are selectable objects that can be reopened with the Text tool, moved,
  scaled, rotated, deleted, and erased as a whole.
- Preserved compatibility with older position-only text notes by upgrading
  them to four-corner object geometry when loaded.
- Fixed hot reload retaining stale package attributes (for example an old
  `ffmpegtools.MediaInfo` class beside a newly loaded `window` module).
- Added configurable, validated playback cache policies with environment
  overrides for decode width, memory, frame capacity, and batch size.
- Increased default display decode quality and cache runway while retaining a
  strict byte budget.
- Added explicit Realtime and Play All Frames playback policies. Play All
  Frames uses deterministic frame-addressed decoding and pauses/resumes audio
  with cache buffering so it cannot continue across a video stall.
- Added copyable playback/cache diagnostics with backend, policy, memory,
  scheduling, buffering, and presentation counters.
- Unified movie and image-sequence caches under the same policy and protected
  oversized current frames from immediate eviction.
- Matched OpenRV's default 25% look-back cache allocation and exposed it as a
  validated preference/environment override.
- Added OpenRV-style Off, Region Cache, and Look-Ahead Cache modes with
  persistent menu selection.
- Added probed audio channel/layout/sample-rate metadata to the Media panel and
  playback diagnostics. Playback preserves the source stream layout; only the
  timeline waveform is intentionally downmixed to mono.
- Added bounded audio drift correction during video-master Play All Frames
  playback while avoiding frequent audible micro-seeks.
- Renamed the application, its About dialog and Maya workspace identifiers to FrameReviewer.
- Added configurable UI region visibility and renamed the side panel to Properties.
- Added independent timeline bookmarks and dominant-color note indicators.
- Added 1-point position, 2-point similarity, 3-point affine, and perspective tracking with smoothed quadrilateral preview and export.
- Corrected crop, annotation and tracking overlay behavior under image flips.
- Improved rolling FFmpeg decoding, cache reuse and end-of-range behavior.
- Improved audio playback for FFmpeg fallback in Maya's Qt5 environment and seek/loop synchronization.
- Coalesced rapid manual seeks, used the audio clock to keep playback smooth during video cache misses, and deferred Qt multimedia teardown beyond sender callbacks.
- Throttled Maya timeline synchronization to avoid evaluation feedback during Play, and retained running workers across interrupted shutdown/reload until they can be stopped safely.

## 3.0.0

- Initial FrameReviewer package name and Maya entrypoints.
