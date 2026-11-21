# Provenance of the 30 fps highlight goldens

`sidecar/test_highlights_frame_rate.py::test_thirty_fps_output_is_byte_identical_to_the_pre_fix_cutter` compares `golden_text()` against `sidecar/fixtures/highlights_golden_30fps_b0d5ad9.txt` (sha256 `82683e1db0bbdb0c869cff102ed8d4c97e30ff24584df7ec8d459a97900cffdf`).

`golden_text()` runs `make_highlights` on 5 synthetic bundles: regular, jitter, two parts, custom pre/post, no-burn and max clips. In each run, ffmpeg is stubbed and every ffprobe call answers `r_frame_rate=30/1`. It records:
- each cut's `-ss`/`-t` strings and the exact float reprs (26 cuts);
- every line printed;
- the full highlights `manifest.json`, minus `generated_ms`;
- every `.vtt`;
- the mono bounds used for clip signing.

The fixture was produced on Nov 12 2026 (CST) by running the **same function against the pre-fix cutter at main `b0d5ad9`** (`FPS = 30.0`, no rate probe). The pre-fix module has no `_probe_run`, so the stub is not applied there.

Reproduce:

```
git worktree add --detach /tmp/hl-prefix b0d5ad9
cp sidecar/test_highlights_frame_rate.py /tmp/hl-prefix/sidecar/
cd /tmp/hl-prefix/sidecar
python -c "import test_highlights_frame_rate as t, sys; sys.stdout.write(t.golden_text())" > golden.txt
sha256sum golden.txt   # 82683e1db0bbdb0c869cff102ed8d4c97e30ff24584df7ec8d459a97900cffdf
```

On the pre-fix module, all 9 other tests in that file fail and the golden test passes. That shows the goldens pin the old 30 fps behaviour and the other tests pin the new rate handling.
