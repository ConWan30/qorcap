# Provenance of the 30 fps golden strings in `src/align.rs`

`thirty_fps_outputs_are_byte_identical_to_the_pre_fix_code` compares the current code with three golden strings: `GOLDEN_FIT_30`, `GOLDEN_MEAS_30` and `GOLDEN_AB_30`. On Nov 12 2026 (CST) those strings were regenerated from the **pre-fix code at main `4888a6319a31885aad388c38001cb4b0adc8c4b5`**, using the same fixtures and an oracle test added there. All three came out byte-identical to the constants checked in on this branch.

Reproduce:

```
git worktree add /tmp/golden-4888a63 4888a6319a31885aad388c38001cb4b0adc8c4b5
cd /tmp/golden-4888a63
git apply golden_oracle.patch            # the patch below
cargo test --locked --lib align::tests::golden_oracle_print -- --nocapture | grep '^GOLDEN' > golden_4888a63.txt
sha256sum golden_4888a63.txt              # 17cf0b155131c38e356e8e2a4e4089f9064f10fed72c154c3486f6595cd3ee41
```

The three output lines are `GOLDEN_FIT_30=<json>`, `GOLDEN_MEAS_30=<json>` and `GOLDEN_AB_30=<json>`. Each `<json>` equals the matching constant in `src/align.rs`. The pre-fix `clock_ab` takes no rate argument, so the oracle calls `clock_ab(&man, &st)`. Everything else in the oracle is identical to the current test.

`golden_oracle.patch` (against `4888a63`):

```diff
diff --git a/src/align.rs b/src/align.rs
index 46b9d3b..a48f168 100644
--- a/src/align.rs
+++ b/src/align.rs
@@ -770,4 +770,18 @@ mod tests {
         let z = clock_ab(&m, &stamps);
         assert!((z["measured"]["vs_fit_signed_p50_ms"].as_i64().unwrap() + 522).abs() <= 2, "{z:#}");
     }
+
+    #[test]
+    fn golden_oracle_print() {
+        let frames: Vec<usize> = (0..120).map(|i| 200 + i * 41).collect();
+        let tr = track(6000, &frames, 6 + 30);
+        let presses: Vec<u64> = frames.iter().map(|&f| (f as f64 * 1000.0 / FPS * 1000.0) as u64).collect();
+        println!("GOLDEN_FIT_30={}", serde_json::to_string(&align_tracks(&[tr], &clock(0.0), &presses, 15)).unwrap());
+        let s = session(520.0, 120, 3, true);
+        println!("GOLDEN_MEAS_30={}", serde_json::to_string(&align_tracks_with(&s.tracks, &s.clocks, &s.presses, 15, &s.stamps)).unwrap());
+        let mut man = vec![json!({"type":"segment","kind":"init","file":"video.mp4","mono_ms":0,"frames":0})];
+        for i in 1..20 { let fr = i * 60; man.push(json!({"type":"segment","kind":"media","file":"video.mp4","mono_ms": fr * 1000 / 30 + 7, "frames": fr})); }
+        let st: Vec<FrameStamp> = (0..300).map(|f| FrameStamp { file: "video.mp4".into(), frame: f, t_mono_us: (f as f64 * 1_000_000.0 / FPS).round() as u64 + 9000 + (f as u64 * 37 % 5000), pts_90k: f as u64 * 3000 }).collect();
+        println!("GOLDEN_AB_30={}", serde_json::to_string(&clock_ab(&man, &st)).unwrap());
+    }
 }
```
