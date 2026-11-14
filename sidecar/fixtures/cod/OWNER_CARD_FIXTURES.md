# Owner-card fixtures

Two small crops from real CoD end boards. They are used by `sidecar/test_qorcap_cod_calib.py`. The source frames are on the box only and are not committed. Each source frame was extracted from that match's mp4 (see MAPPING.md beside the frames).

Steps for both crops:

- Crop x 150-820, y 390-668 of the 1280x720 frame (670x278). Tests paste the crop back at that offset.
- On rows y 420-600, blank the name column (x < 330) except Qortrola30's row (row y -6 .. +22).
- Blank x >= 625 for y < 600, which is the other team's board.
- Write the result as PNG, compression 9, with OpenCV.

Rebuilding from the source frame with these steps gives the same sha256.

| fixture | sha256 | source match | seq / video frame | source frame sha256 | edits beyond the steps above |
|---|---|---|---|---|---|
| `owner_card_214647_crop.png` | `593a491a190a56028853c0077050fff5c6398924a63b198de67bc87070808ac8` | match-20261113-214647 (Kill Confirmed) | seq 1840, frame 27599 (`seq1840_f27599.png`) | `acf2ba9a2716c8d3719b9d8ddc927e3737fe61a1870f5b6fb6f6a25d97afe011` | none. The personal card is Qortrola30's own. |
| `owner_card_211713_crop.png` | `33e989fa8bdc89e428433d058b918545c1494b564a93b3a2768eb71ae2146580` | match-20261113-211713 (Hijack) | seq 3006, frame 45085 (`seq3006_f45085.png`) | `9287d984664523635481db8660eb52ef1beb63d1b86cae68a644dd365941acef` | Card edits listed below. |

Card edits on `owner_card_211713_crop.png`:

- The real card named another player. That name is removed, along with the card's level and its ELIMINATIONS and DEATHS digits: blanked at frame x 372-560 y 620-646, x 340-372 y 644-672, x 620-650 y 646-668, and x 760-790 y 646-668.
- A made-up name `Rival_7Tag` is drawn at (379, 637). It uses cv2.putText, FONT_HERSHEY_SIMPLEX 0.42, BGR (40, 160, 220), thickness 1, LINE_AA.
- The committed canonical row-digit masks write `27` at (768, 650) and `40` at (628, 650), in BGR (235, 235, 235).
- The card's DEATHS label, the board headers (SCORE / CAPTURES / DATA UPLOADED / KILLS) and Qortrola30's row are the real pixels.

The pack masks `mode_captures`, `mode_data_uploaded` and `score` were cut from the 211713 source frame. `owner_card_tags` was cut from the 214647 source frame. Both are tuning samples and are not held-out.
