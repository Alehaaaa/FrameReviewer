# Playback & scrub audit against xStudio and OpenRV

Audit snapshot: 2026-11-03. xStudio `main` tree `bb05faa3`;
OpenRV `main` tree `124c9e8b`. FrameReviewer:
`feature/cache-first-scrub-speed-suffix-2026-11-03`.

## Verified implementation comparisons

| Concern | xStudio / OpenRV | FrameReviewer state |
| --- | --- | --- |
| Cached frame display | xStudio looks up a source-specific `MediaKey` in its global image cache before reading. OpenRV's `CacheIPNode` similarly checks out resident buffers. | Cached `QPixmap` lookup happens before background FFmpeg; the rolling LOOKAHEAD window no longer destroys in-budget cached frames merely because the cursor moved. |
| Urgent vs prefetch | xStudio has a separate urgent image reader and asynchronous precache workers. The latest request from each playhead supersedes stale pending reads. | The compositor now keeps only the newest urgent playhead frame; current-frame `QImage` uploads jump ahead of lookahead images. The FFmpeg worker remains bounded and asynchronous. |
| Scrub coalescing | xStudio dispatches a lazy push request when scrubbing faster than its image reader; OpenRV can present resident cache without re-evaluating the source. | Timeline emits only changed frames, reports the playhead immediately, and schedules uncached FFmpeg seeks at a bounded 45 ms cadence rather than restarting a trailing debounce for every mouse pixel. |
| Old decoding after a jump | xStudio prunes stale requests and limits competing compressed-video reads. | Outdated QImages outside the active window are released without QPixmap conversion; superseded request serials/generations prevent stale presentation. Previous decoded hot frames remain until bounded eviction. |
| H.264/ProRes sequential decode | xStudio explicitly prefers a single sequential read worker for GOP codecs, while permitting parallel image-sequence readers. | FrameReviewer uses one active FFmpeg decode worker per movie, with a cap on workers still finishing cancellation. A full independent urgent decoder is *not* implemented. |
| Virtual playlist | xStudio playhead resolves frame identities across playlist items; OpenRV uses an image-processing graph. | `CompositeSource` maps one global frame to independent reader/source IDs. There is **no FFmpeg concatenation** for preview. |
| Timeline indicators | xStudio draws a green residency bar; OpenRV exposes cache ranges and a timeline overlay. | FrameReviewer displays source clip stripes separately from cache coverage. Cache ranges include source-owned frames in a virtual sequence. |
| Scrub audio / Maya | Independent playhead/audio handling in xStudio. | During a mouse drag, render the decoded frame immediately when cached, postpone explicit device PCM seek and Maya currentTime writes until release. No fabricated audio sync is claimed. |
| Speed widget | N/A to media-cache design. | Native editable Qt combo; 0.25× wheel steps and preset selection. The visual × is an unselectable QLabel child of the QLineEdit, not part of the user-editable number. |

## Relevant upstream implementation (read, not copied)

- xStudio request deduplication, stale-request pruning and sequential H.264 limiting:
  https://github.com/AcademySoftwareFoundation/xstudio/blob/main/src/media_reader/src/frame_request_queue.cpp
- xStudio cache-first immediate lookup, urgent worker and asynchronous precache:
  https://github.com/AcademySoftwareFoundation/xstudio/blob/main/src/media_reader/src/cacheing_media_reader_actor.cpp
- xStudio shared image cache, hot/resident frame protection and global budget:
  https://github.com/AcademySoftwareFoundation/xstudio/blob/main/src/media_cache/src/media_cache_actor.cpp
- xStudio playhead lazy fetch and latest request supersession:
  https://github.com/AcademySoftwareFoundation/xstudio/blob/main/src/playhead/src/sub_playhead.cpp
- xStudio user documentation on playhead/cache and timeline indicators:
  https://github.com/AcademySoftwareFoundation/xstudio/blob/main/docs/user_docs/workflow/playback.rst
- OpenRV `CacheIPNode` resident-frame reuse:
  https://github.com/AcademySoftwareFoundation/OpenRV/blob/main/src/lib/ip/IPCore/CacheIPNode.cpp
- OpenRV cache display in the timeline:
  https://github.com/AcademySoftwareFoundation/OpenRV/blob/main/src/lib/app/mu_rvui/timeline.mu

## Critical remaining gaps: not production-validated

1. **Cold EXR / image-sequence decoding can still block the Qt GUI.**
   `SequenceSource.get()` performs bounded EXR reads, colour conversion and
   `QPixmap` construction synchronously. xStudio decodes EXRs with independent
   reader workers when its plugin allows parallel reads. Moving this safely
   off-thread requires carrying immutable source `FramePayload` + QImage
   through a cancellable worker, then constructing QPixmap on the Qt GUI
   thread. Existing SHA256 identity checks, source-window/PAR, layer/part
   selection, hold/hole policies and precision parity must survive unchanged.

2. **No global memory arbiter across all clip readers.**
   xStudio has one budgeted image cache keyed by source frame identity.
   FrameReviewer currently bounds each `VideoFrameSource` and
   `SequenceSource` separately; a long playlist can use more aggregate RAM
   than the intended cache budget. A shared cache must avoid evicting in-use
   frame payloads, preserve generation/colour separation and never surface
   stale Source bytes.

3. **The native Qt multimedia backend is not a source-owned decoded-frame
   cache.** Its seek coalescing is independent of the FFmpeg FramePump cache.
   A native-codec scrub may still be limited by the platform decoder. Do not
   claim the same latency as xStudio without Maya/codec benchmarks.

4. **GPU upload is still on the GUI thread.** QImage decoding for movies is
   threaded but QPixmap uploads are batched; high-res frame presentation may
   still bottleneck on Maya's graphics workload. Limit changes by measured
   GPU timings, not blind batch-size increases.

## Regression evidence and measurement plan

CI runs the Qt offscreen timeline, request supersession, in-budget cache
retention, CACHE_OFF policy, priority upload, obsolete image rejection and
editable speed suffix tests. Python 3.9/3.11/3.12 also compile and run the
headless regression matrix. These are *functional checks*, not end-to-end
performance measurements.

On Maya 2023 / Python 3.9 / PySide2, compare repeated scrub passes over
resident frames versus cold frames, first vs subsequent visits to clips,
10/100-clip playlists, frame-accurate EXR and mixed-fps material, and
interactions with native Source precision, notes, audio and stabilization.
Record median/p95 input-to-presented-frame latency, decode worker count,
QPixmap cache bytes, actual total RSS and stale-frame occurrences. No claim
of 'instant' uncached EXR or native decoding before that validation.
