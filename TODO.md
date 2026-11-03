# Before calling this stable

## Config decoupling
- [ ] inbox / library / index paths from config, not CLI-only
- [ ] concurrency, timeout, retry counts
- [ ] size + dimension thresholds
- [ ] mood/scene enums + schema version in the index
- [ ] describer adapter registry (no vendor in core)
- [ ] mock describer that needs no credentials

## Consistency
- [ ] do not process a file that is still being written
- [ ] multi-process ingest keeps sha dedupe correct
- [ ] crash between `move` and index append is recoverable
- [ ] separate counters for network fail / parse fail / not-a-meme
- [ ] quarantine folder for repeatedly failing images
- [ ] `--verify` to rebuild or check the index
- [ ] never trust model text for filenames — strip separators, control chars, cap length
- [ ] keep original filename ↔ hash mapping

## JSON validation
- [ ] `is_meme` must be a real bool, not the string `"false"`
- [ ] field type / length / list-size checks
- [ ] define: drop the bad tag, or reject the whole record (do not silently
      produce partial records)
- [ ] handle model output wrapped in code fences or with prose around the JSON

## Formats
- [ ] state whether animated GIF/WebP are supported, and which frame is described
- [ ] list which formats yield reliable dimensions
- [ ] document external CLI deps (`identify`/`ffprobe`) and API deps

## Data & rights
- [ ] sample images must be self-made or clearly licensed
- [ ] code license and image license kept separate
- [ ] README warning about uploading to vision services
- [ ] never ship a real personal meme library as a test fixture
