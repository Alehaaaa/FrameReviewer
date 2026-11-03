# Cache-first timeline scrubbing

FrameReviewer uses a virtual playlist with independent source decoders. The
export queue has no role in playback; the "rendered" frames shown during a drag
come from the source's decoded RAM cache (or asynchronously from FFmpeg).

## References and deliberate implementation differences

- **xStudio**: `FrameRequestQueue` deduplicates and prioritizes frame requests;
  stale requests are pruned and outstanding compressed-media decode is kept
  sequential, rather than spawning multiple H.264 workers out of order.
  See
  https://github.com/AcademySoftwareFoundation/xstudio/blob/main/src/media_reader/src/frame_request_queue.cpp
  and
  https://github.com/AcademySoftwareFoundation/xstudio/blob/main/src/media_reader/src/cacheing_media_reader_actor.cpp.
- **xStudio**: the immediate viewer request checks global image cache first;
  uncached reads are asynchronous and superseded scrub targets replace stale
  per-playhead urgent requests. Its playback documentation describes forward
  prefetch and timeline cache bands:
  https://github.com/AcademySoftwareFoundation/xstudio/blob/main/docs/user_docs/workflow/playback.rst.
- **OpenRV**: `CacheIPNode` reuses cached image buffers and differentiates
  display-time cache misses from background evaluations:
  https://github.com/AcademySoftwareFoundation/OpenRV/blob/main/src/lib/ip/IPCore/CacheIPNode.cpp
  and timeline cache visualization:
  https://github.com/AcademySoftwareFoundation/OpenRV/blob/main/src/lib/app/mu_rvui/timeline.mu.

FrameReviewer keeps its own bounded decoder cache and generation-safe Qt
presentation. We deliberately do not import either application's much larger
actor or graph system, create temporary concatenated movies, or block Maya's
GUI for uncached frames. Cached backward seeks do not cancel a live worker;
uncached seek targets are coalesced; drag previews defer audio-device and Maya
timeline synchronization until mouse release. The UI reflects the target frame
immediately, while an asynchronous decode may still be pending.

## Limitations

These are code-level optimizations, not a guarantee that *uncached* pixels
appear instantaneously. Qt's native multimedia backend has its own decoder
and position-coalescing behavior; the FFmpeg rolling cache covers the
FramePump backend. Verify scrub responsiveness inside Maya 2023/PySide2
with actual codec and EXR samples before claiming production readiness.
