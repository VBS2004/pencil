/*
 * On-device narration with Supertonic 3 (int8 ONNX) in the browser. No server, no API key.
 *
 * The pipeline (duration predictor -> text encoder -> vector estimator x N steps -> vocoder) follows the official
 * example at github.com/supertone-inc/supertonic (web/helper.js), MIT License, Copyright (c) 2026 Supertone Inc.
 * Reworked here for typed arrays, Cache Storage loading and per-word timing.
 *
 * The model weights are NOT MIT: they are BigScience OpenRAIL-M. Use-based restrictions apply (for example: no
 * non-consensual impersonation, no undisclosed machine-generated content) and must be passed on. See
 * tts/models/supertonic-3-int8/LICENSE.
 *
 * Needs a global `ort` (onnxruntime-web, e.g. <script src="vendor/ort/ort.wasm.min.js">).
 */

export const LANGS = ['en', 'ko', 'ja', 'ar', 'bg', 'cs', 'da', 'de', 'el', 'es', 'et', 'fi', 'fr', 'hi', 'hr', 'hu', 'id', 'it', 'lt', 'lv', 'nl', 'pl', 'pt', 'ro', 'ru', 'sk', 'sl', 'sv', 'tr', 'uk', 'vi'];

/* ---------- text front end (G2P-free: NFKD + codepoint lookup) ---------- */

const EMOJI = /[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F700}-\u{1F77F}\u{1F780}-\u{1F7FF}\u{1F800}-\u{1F8FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F1E6}-\u{1F1FF}]+/gu;
const SUBS = { '–': '-', '‑': '-', '—': '-', _: ' ', '“': '"', '”': '"', '‘': "'", '’': "'", '´': "'", '`': "'", '[': ' ', ']': ' ', '|': ' ', '/': ' ', '#': ' ', '→': ' ', '←': ' ', '=': ' equals ', '+': ' plus ' };

export function preprocess(text, lang) {
  if (!LANGS.includes(lang)) throw new Error(`Unsupported language "${lang}"`);
  let t = text.normalize('NFKD').replace(EMOJI, '');
  for (const [k, v] of Object.entries(SUBS)) t = t.replaceAll(k, v);
  t = t.replace(/[♥☆♡©\\]/g, '').replaceAll('@', ' at ').replaceAll('e.g.,', 'for example, ').replaceAll('i.e.,', 'that is, ');
  t = t.replace(/ ,/g, ',').replace(/ \./g, '.').replace(/ !/g, '!').replace(/ \?/g, '?').replace(/ ;/g, ';').replace(/ :/g, ':').replace(/ '/g, "'");
  t = t.replace(/"{2,}/g, '"').replace(/'{2,}/g, "'").replace(/\s+/g, ' ').trim();
  if (!/[.!?;:,'")\]}…。」』】〉》›»]$/.test(t)) t += '.';
  return `<${lang}>${t}</${lang}>`;
}

/** Split into sentence-sized chunks (the model reads each chunk separately). */
export function chunkText(text, maxLen = 300) {
  const chunks = [];
  for (const para of text.trim().split(/\n\s*\n+/).filter((p) => p.trim())) {
    const sentences = para.trim().split(/(?<!Mr\.|Mrs\.|Ms\.|Dr\.|Prof\.|Sr\.|Jr\.|etc\.|e\.g\.|i\.e\.|vs\.|St\.)(?<!\b[A-Z]\.)(?<=[.!?])\s+/);
    let cur = '';
    for (const s of sentences) {
      if (cur.length + s.length + 1 <= maxLen) cur += (cur ? ' ' : '') + s;
      else { if (cur) chunks.push(cur.trim()); cur = s; }
    }
    if (cur) chunks.push(cur.trim());
  }
  return chunks;
}

/* ---------- timing: spread a chunk's audio time over its words ---------- */

/** Weight of a word for timing: its length, plus a pause after clause/sentence punctuation. */
function wordWeight(w) {
  let weight = w.replace(/[^\p{L}\p{N}]/gu, '').length + 1.5;
  if (/[.!?…]$/.test(w)) weight += 5;
  else if (/[,;:]$/.test(w)) weight += 2.5;
  return weight;
}

export function allocateWordTimes(words, start, end) {
  const weights = words.map(wordWeight), total = weights.reduce((a, b) => a + b, 0) || 1;
  let t = start;
  return words.map((word, i) => {
    const d = ((end - start) * weights[i]) / total, entry = { word, start: t, end: t + d };
    t += d;
    return entry;
  });
}

/* ---------- WAV ---------- */

export function encodeWav(samples, sampleRate) {
  const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) v.setInt16(44 + i * 2, Math.round(Math.max(-1, Math.min(1, samples[i])) * 32767), true);
  return buf;
}

/* ---------- model ---------- */

/** Fetch a file once, keep it in Cache Storage, and return its bytes (so the next visit needs no download). */
async function cachedBytes(url, cacheName) {
  const cache = cacheName && 'caches' in globalThis ? await caches.open(cacheName) : null; // null cacheName = no caching
  let res = cache && (await cache.match(url));
  const fromCache = !!res;
  if (!res) {
    res = await fetch(url);
    if (!res.ok) throw new Error(`Could not load ${url}: HTTP ${res.status}`);
    if (cache) await cache.put(url, res.clone());
  }
  return { bytes: new Uint8Array(await res.arrayBuffer()), fromCache };
}

export class SupertonicTTS {
  static async load({ modelBase, ortBase, cacheName = 'pencil-tts-supertonic3-int8-v1', onProgress = () => {} }) {
    const ort = globalThis.ort;
    if (!ort) throw new Error('onnxruntime-web (global ort) is not loaded');
    ort.env.wasm.wasmPaths = new URL(ortBase, document.baseURI).href; // must be absolute for the wasm module import
    ort.env.wasm.numThreads = globalThis.crossOriginIsolated ? Math.min(4, navigator.hardwareConcurrency || 2) : 1;

    const t0 = performance.now();
    const names = ['duration_predictor', 'text_encoder', 'vector_estimator', 'vocoder'];
    const sessions = {};
    let allCached = true;
    for (const [i, n] of names.entries()) {
      onProgress(`loading ${n} (${i + 1}/${names.length})`);
      const { bytes, fromCache } = await cachedBytes(`${modelBase}/onnx/${n}.onnx`, cacheName);
      allCached = allCached && fromCache;
      sessions[n] = await ort.InferenceSession.create(bytes, { executionProviders: ['wasm'], graphOptimizationLevel: 'all' });
    }
    const cfg = await (await fetch(`${modelBase}/onnx/tts.json`)).json();
    const indexer = await (await fetch(`${modelBase}/onnx/unicode_indexer.json`)).json();
    const tts = new SupertonicTTS(ort, cfg, indexer, sessions);
    tts.loadSeconds = (performance.now() - t0) / 1000;
    tts.fromCache = allCached;
    tts.threads = ort.env.wasm.numThreads;
    return tts;
  }

  constructor(ort, cfg, indexer, sessions) {
    this.ort = ort; this.cfg = cfg; this.indexer = indexer; this.s = sessions;
    this.sampleRate = cfg.ae.sample_rate;
  }

  async loadVoice(url) {
    const j = await (await fetch(url)).json();
    const T = (o) => new this.ort.Tensor('float32', Float32Array.from(o.data.flat(Infinity)), o.dims);
    return { ttl: T(j.style_ttl), dp: T(j.style_dp) };
  }

  _ids(text, lang) {
    const t = preprocess(text, lang);
    const ids = new BigInt64Array(t.length);
    for (let i = 0; i < t.length; i++) { const cp = t.codePointAt(i); ids[i] = BigInt(cp < this.indexer.length ? this.indexer[cp] : -1); }
    return ids;
  }

  /** One chunk of text -> { samples, seconds }. */
  async synthChunk(text, lang, voice, steps, speed) {
    const { Tensor } = this.ort, ids = this._ids(text, lang), L = ids.length;
    const textIds = new Tensor('int64', ids, [1, L]);
    const textMask = new Tensor('float32', new Float32Array(L).fill(1), [1, 1, L]);

    const dp = await this.s.duration_predictor.run({ text_ids: textIds, style_dp: voice.dp, text_mask: textMask });
    const seconds = dp.duration.data[0] / speed;
    const enc = await this.s.text_encoder.run({ text_ids: textIds, style_ttl: voice.ttl, text_mask: textMask });

    const { base_chunk_size: base } = this.cfg.ae, { chunk_compress_factor: cc, latent_dim: ld } = this.cfg.ttl;
    const wavLen = Math.floor(seconds * this.sampleRate), chunk = base * cc;
    const latLen = Math.max(1, Math.ceil(wavLen / chunk)), dim = ld * cc;
    let xt = new Float32Array(dim * latLen);
    for (let i = 0; i < xt.length; i++) xt[i] = Math.sqrt(-2 * Math.log(Math.max(1e-4, Math.random()))) * Math.cos(2 * Math.PI * Math.random());
    const latMask = new Tensor('float32', new Float32Array(latLen).fill(1), [1, 1, latLen]);
    const total = new Tensor('float32', new Float32Array([steps]), [1]);

    for (let step = 0; step < steps; step++) {
      const out = await this.s.vector_estimator.run({
        noisy_latent: new Tensor('float32', xt, [1, dim, latLen]), text_emb: enc.text_emb, style_ttl: voice.ttl,
        latent_mask: latMask, text_mask: textMask, current_step: new Tensor('float32', new Float32Array([step]), [1]), total_step: total,
      });
      xt = Float32Array.from(out.denoised_latent.data);
    }
    const voc = await this.s.vocoder.run({ latent: new Tensor('float32', xt, [1, dim, latLen]) });
    const wav = voc.wav_tts.data;
    return { samples: wav.length > wavLen ? wav.slice(0, wavLen) : Float32Array.from(wav), seconds };
  }

  /**
   * Narrate `text`. Returns the audio plus when each word is spoken (estimated within each sentence).
   *   { samples, sampleRate, seconds, words: [{index, word, start, end}], chunks: [{text, start, end}] }
   */
  async speak(text, { voice, lang = 'en', steps = 8, speed = 1.05, gap = 0.18, onChunk = () => {} } = {}) {
    // One sentence per chunk (maxLen 1 never packs sentences together): the audio length of each sentence is then
    // known exactly, which keeps the per-word timing estimate tight.
    const chunks = chunkText(text, 1);
    const parts = [], words = [], meta = [];
    let t = 0, wi = 0;
    for (const [i, c] of chunks.entries()) {
      const { samples, seconds } = await this.synthChunk(c, lang, voice, steps, speed);
      const start = t, end = t + samples.length / this.sampleRate;
      for (const w of allocateWordTimes(c.split(/\s+/).filter(Boolean), start, end)) words.push({ index: wi++, ...w });
      meta.push({ text: c, start, end });
      parts.push(samples);
      t = end;
      if (i < chunks.length - 1) { const g = new Float32Array(Math.floor(gap * this.sampleRate)); parts.push(g); t += gap; }
      onChunk(i + 1, chunks.length);
    }
    const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return { samples: out, sampleRate: this.sampleRate, seconds: out.length / this.sampleRate, words, chunks: meta };
  }
}
