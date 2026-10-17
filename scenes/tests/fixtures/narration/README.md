# Narration service fixtures

Response bodies in the shapes the two services document, used by
`tests/unit/narration.test.ts` through a fake `fetch` and a loopback server:

- `kokoro-captioned-speech.json`: Kokoro-FastAPI `POST /dev/captioned_speech`
  (`CaptionedSpeechResponse` in remsky/Kokoro-FastAPI
  `api/src/structures/schemas.py`: `audio` base64, `audio_format`,
  `timestamps: [{word, start_time, end_time}]`), keyed by the sentence sent.
  `audio` is `"<generated>"`: the test splices in a generated tone WAV of the
  sentence's length, so the fixture stays small.
- `whisper-verbose.json`: an OpenAI-compatible `POST /v1/audio/transcriptions`
  answer with `response_format=verbose_json` and word timestamps (`text`,
  `duration`, `words: [{word, start, end}]`).

These are SHAPE fixtures written from the services' schemas, not recordings:
no Kokoro-FastAPI or Whisper endpoint was reachable from the fleet when they
were written (2026-10-17). Replace them with recorded answers once one is.
