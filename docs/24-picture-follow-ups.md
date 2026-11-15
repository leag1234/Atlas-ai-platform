# Picture follow-ups: understood, honest, and kept (design note, 2026-11-15)

Scope: the follow-ups a user gives after a picture (CPU image server, and
painting by code). Code: `services/orchestrator/pictures/dialogue.py`,
`cpu_turn.change`, `followup.py`, `rewrite.py`, `infra/pictures-cpu/gate.py`.
Tests: `tests/test_picture_followups.py`.

## 1. What was measured (one staging conversation, a dozen follow-ups)

1. Each change rewrote the prompt from the bare follow-up and drew the whole
   picture again with the same seed. Composition drifted at every change: a
   face detail made the clothes asked for earlier disappear.
2. Follow-ups without a visual instruction (a retry, an "apply it", a
   complaint that nothing changed) were given alone to the rewrite. Measured
   on the staging gateway with the rewrite role and a fictional request:
   four of five such phrasings returned the previous prompt unchanged, and
   the fifth was misread as a change of the subject. Same prompt and same
   seed gave a byte-identical picture: five versions held the same picture
   while each answer said it had been changed.
3. Requests for a critique or a plan went to the conversation model, which
   critiqued pictures it never saw and promised a drawing that no step of the
   turn made.
4. Short retries went to the HTML artifact editor, which failed with
   `artifact_conflict` (the chat showed an older version than the latest).

## 2. What changed

**Understanding (dialogue.py).** When the conversation's current artifact is
a picture set of this user, shown in one of the last answers, one small JSON
call (the `picture_rewrite` role, temperature 0, about 0.0005 EUR) reads the
last turns as data and classifies the last message: `change`, `review`, `new`
or `other`, in any language and spelling. For a change it writes the whole
change in English from the conversation (corrections asked and not yet made,
the plan Atlas stated when the user says to apply it), and the list of every
requirement the user stated. Router kinds file, run, remote and research skip
the call. If the call fails, the former behaviour holds. A picture set is never
handed to the HTML artifact editor: anything that is not about the picture is
routed as if there were no current artifact.

**Honesty (cpu_turn.change, followup.py).**

- an empty change (a plain retry) is a new draw of the same description with a
  new seed, said as such ("a new draw, not a corrected picture");
- a prompt the rewrite leaves unchanged gets one stronger rewrite; if it is
  still unchanged, the same new-draw rule applies;
- a picture whose bytes equal the current one (sha256 of the server's PNG)
  makes no new version and is said plainly; the same holds for painting by
  code when the stored document comes back identical;
- a chat that shows an older version of the picture changes the latest one.

**Evidence (dialogue.review).** A critique, a "why" or a plan request is
answered by one vision call on the current picture (the gateway's existing
`locate` role, as `cpu_checks`), given the requirements, the description and
how changes are made. Code writes the answer from the call's fields (shown,
missing, other defects, plan), adds how changes work, and ends with an
invitation to apply the plan, never a promise. Without the picture or the
vision call, the answer says it could not look and lists only what was asked.

**Keeping what was validated.** Every change gives the rewrite the earlier
changes and the requirements, with the rule to copy every untouched sentence
of the previous prompt word for word and never drop a subject, garment,
colour, pose or visible body part. This is the prompt-level mitigation in use
today. The reference-image edit below is the structural one.

## 3. Survey: editing the picture instead of drawing it again

Owner rule: look at existing open work first. Requirements: open licence that
allows commercial use, CPU only (COMPUTE3-X16C-32G, no GPU), about 150 s per
picture at most, maintained runtime.

| Option | Licence | Size, hardware | CPU feasibility | Verdict |
| :--- | :--- | :--- | :--- | :--- |
| FLUX.2 [klein] 4B, reference image (the model already served) | Apache-2.0 | 4B, "fits in ~13GB VRAM"; same Q4_K weights as today | The pinned stable-diffusion.cpp supports it: `docs/flux2.md` shows a klein edit with `-r` at 4 steps; the server's sdapi parses `extra_images` into `ref_images` (`examples/server/routes_sdapi.cpp`). Cost estimate: the reference doubles the image tokens, so about 2 to 2.5 times a new picture (23 s), about 50 to 60 s. | Chosen. No new weights, no new service |
| img2img (init image, denoising strength), same model | Apache-2.0 | same | Supported by the same server (`/sdapi/v1/img2img`, `init_images`, `denoising_strength`); faster than an edit | Keeps the layout but changes little at low strength and everything at high strength: not an instruction editor. Kept as a possible fallback, not wired |
| OmniGen2 | Apache-2.0 | about 17 GB VRAM; CPU offload is for low-VRAM GPUs | No GGUF or stable-diffusion.cpp support | Out (GPU-shaped) |
| Step1X-Edit | Apache-2.0 | 42 to 50 GB GPU memory, 18 GB with FP8 and offload; 5 to 22 s on an H800 | No CPU runtime | Out |
| Qwen-Image-Edit | Apache-2.0 | 20B parameters | Supported by stable-diffusion.cpp (`docs/qwen_image_edit.md`), but five times the klein transformer on 16 vCPU: well beyond 150 s (estimate, not measured) | Out for this server |
| FLUX.1 Kontext dev | FLUX.1 [dev] Non-Commercial License | 12B | Supported by stable-diffusion.cpp | Out (licence) |

What is implemented:

- `infra/pictures-cpu/gate.py`: `/v1/render` accepts an optional `reference`
  picture (PNG or JPEG, checked, redrawn at 512 px by the gate) and passes it
  to the engine as `extra_images`; edits have their own measured seconds per
  picture in the queue; `/v1/health` says `"edit": true` for klein;
- `cpu_server.render_edit` and `cpu_server.edits(status)`;
- `cpu_turn.change`: when the server says it edits, the current picture's own
  render (stored with the set, before code set words or labels) is sent with
  the rewrite's `edit` instruction plus a fixed "keep everything else" line; a
  failed edit falls back to drawing the changed prompt in the same turn.

Not yet measured: the edit time and quality on the image server. The running
server is the image built before this change and answers without `"edit"`, so
the text-to-image path stays in use until the gate is rebuilt and redeployed
(`infra/pictures-cpu/install.sh`, an owner or supervisor step). The gate logs
`render edit=True ... render_s=` for each edit: the first edits give the
measure. If edits exceed the turn's CPU budget, the client falls back to the
text-to-image path by itself (the server's own `seconds_per_edit`).

## 4. Sources

- FLUX.2 [klein] 4B model card (licence, unified generation and editing,
  multi-reference): https://huggingface.co/black-forest-labs/FLUX.2-klein-4B
- stable-diffusion.cpp at the pinned commit
  5d787408c0e053568c4558e3c9043e8ebabd4cae: `docs/flux2.md` (klein edit
  example), `docs/edit.md` (supported edit models and presets),
  `examples/server/routes_sdapi.cpp` (`extra_images`, `init_images`,
  `denoising_strength`): https://github.com/leejet/stable-diffusion.cpp
- OmniGen2 (licence, VRAM, offload): https://github.com/VectorSpaceLab/OmniGen2
- Step1X-Edit (licence, GPU memory and timings):
  https://github.com/stepfun-ai/Step1X-Edit
- Qwen-Image-Edit (licence, size): https://huggingface.co/Qwen/Qwen-Image-Edit
- FLUX.1 Kontext dev (licence):
  https://huggingface.co/black-forest-labs/FLUX.1-Kontext-dev
