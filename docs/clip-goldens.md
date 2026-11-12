# Provenance of the 30 fps clip cut-window goldens in `src/clip.rs`

The test `thirty_fps_cut_windows_are_bit_identical_to_the_pre_fix_code` compares the new rate-aware `cut_window(.., fps = 30.0)` with `CLIP_GOLDEN_30`. On Nov 12 2026 (CST), that string was produced by the **pre-fix code at main `8c310ca`**, which used the old `const FPS: f64 = 30.0`. It was run on the same 27 fixtures (`golden_cut_cases()`) through an oracle test.

Each golden line records, for one fixture:
- the start and end frames;
- the exact IEEE-754 bits of `start_sec` and `dur_sec`;
- the `-ss` / `-t` strings passed to ffmpeg.

Reproduce:

```
git worktree add /tmp/clip-golden 8c310ca
cd /tmp/clip-golden
git apply clip_golden_oracle.patch      # the patch below
cargo test --locked --lib clip::tests::clip_golden_oracle_print -- --nocapture | grep '^CLIPGOLDEN' > clip_golden_8c310ca.txt
sha256sum clip_golden_8c310ca.txt        # 89b7b42cb4c87f1581c14df8efe5aba237a3b045dc583bcf04ad6d625b1d0502
```

`clip_golden_oracle.patch` (against `8c310ca`):

```diff
diff --git a/src/clip.rs b/src/clip.rs
index a09db5b..cc07ecf 100644
--- a/src/clip.rs
+++ b/src/clip.rs
@@ -1361,4 +1361,36 @@ mod tests {
         std::fs::create_dir_all(&p).unwrap();
         p
     }
+    /// Fixtures for the 30 fps cut-window golden (shared verbatim with the pre-fix oracle).
+    fn golden_cut_cases() -> Vec<(Vec<Seg>, u64, u64)> {
+        let s30 = |n: u64, init: bool, off: u64| -> Vec<Seg> {
+            let mut v = vec![];
+            if init { v.push(seg(0, "init", "video.mp4", off, 0)); }
+            for i in 1..=n { v.push(seg(i, "media", "video.mp4", off + i * 2000, (i * 60) as i64)); }
+            v
+        };
+        let mut c = vec![];
+        for (s, e) in [(0u64, 1000u64), (500, 1000), (1234, 5678), (1999, 2001), (3333, 9999), (100, 19_000), (25_000, 30_000)] {
+            c.push((s30(10, true, 0), s, e));
+            c.push((s30(10, true, 700), s, e));
+            c.push((s30(10, false, 0), s, e));
+        }
+        // irregular fragments (ragged frame counts) and an unfitted first fragment
+        let rag = vec![seg(0, "init", "video.mp4", 0, 0), seg(1, "media", "video.mp4", 1001, 31), seg(2, "media", "video.mp4", 2003, 59),
+            seg(3, "media", "video.mp4", 2990, 91), seg(4, "media", "video.mp4", 4100, 122)];
+        for (s, e) in [(0u64, 4100u64), (10, 20), (1500, 2500), (2995, 4000)] { c.push((rag.clone(), s, e)); }
+        // zero frames everywhere: the ms fallback
+        let zero = vec![seg(0, "init", "video.mp4", 0, 0), seg(1, "media", "video.mp4", 1000, 0), seg(2, "media", "video.mp4", 2000, 0)];
+        for (s, e) in [(0u64, 1000u64), (250, 1750)] { c.push((zero.clone(), s, e)); }
+        c
+    }
+
+    #[test]
+    fn clip_golden_oracle_print() {
+        for (i, (segs, s, e)) in golden_cut_cases().iter().enumerate() {
+            let (a, b) = frames_for(segs, *s, *e).unwrap();
+            let (ss, d) = cut_window(a, b, *s, *e);
+            println!("CLIPGOLDEN {i} {a} {b} {:016x} {:016x} {ss:.3} {d:.3}", ss.to_bits(), d.to_bits());
+        }
+    }
 }
```
