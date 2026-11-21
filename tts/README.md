# On-device narration (experimental)

`supertonic.js` runs the Supertonic 3 TTS model in the browser with onnxruntime-web (WASM, int8).
It is a standalone test page; `player.html` still uses the browser's built-in speech synthesis.

The model files and runtime are not in this repo. To try it:

1. Put the onnxruntime-web files (`ort.wasm.min.js`, `ort-wasm-simd-threaded.mjs`,
   `ort-wasm-simd-threaded.wasm`) in `tts/vendor/ort/` (they ship in the `onnxruntime-web` npm package).
2. Download the int8 Supertonic 3 ONNX files (`duration_predictor`, `text_encoder`,
   `vector_estimator`, `vocoder`, plus `tts.json`, `unicode_indexer.json` and a voice style JSON) into
   `tts/models/supertonic-3-sherpa/` using the layout `onnx/*.onnx`, `onnx/*.json`, `voice_styles/M1.json`.
   Check the model's own license before redistributing the weights.
3. `python tts/serve.py 8766`, then open `http://127.0.0.1:8766/tts/test.html`.
   The server must send COOP/COEP headers (`serve.py` does) so WASM threads work.

`tts/run_tts_test.py lesson.json --beats 1 --steps 8 --model supertonic-3-sherpa` renders narration
to WAV through headless Chrome (needs `pip install playwright` and a system Chrome).
