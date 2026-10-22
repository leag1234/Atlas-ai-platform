# M18 image comparison

Source: real local GPU worker, 2026-09-13; seed 618092026, identical rewritten
prompt, 1024×1024, guidance zero, sequence limit 512. Infrastructure rate
EUR1.46988/hour. Raw measurements remain in BRAIN/m18-benchmark.json.

Original request (translated from French): create an image of a dancing cat and
dog, both wearing roller skates and pink glasses.

Rewritten prompt:

```
Subjects: A cat and a dog dancing together.
Attributes: Both subjects are wearing roller skates and pink glasses.
Scene: A dynamic setting capturing the motion of the dance.
Style: Vibrant, playful, and whimsical illustration.
```

| Steps | Worker seconds | Dancing cat and dog | Roller skates | Pink glasses | Image |
|---|---:|---|---|---|---|
| 4 | 3.1594 | present | present | present | [4 steps](4-steps.png) |
| 8 | 4.9288 | present | present | present | [8 steps](8-steps.png) |

These findings are the implementing agent's direct visual inspection of both
PNG outputs, not independent human scores. Both satisfy the three visible
constraints on this one prompt/seed. Four steps remains the default: equal
observed constraint fidelity with lower latency. This small comparison does not
establish a general quality advantage. The public J14 journey completed in this session. Its vision-provider judgment
found all three constraints present (3/3). The original prompt, rewritten prompt,
random seed, and individual judgments are recorded in [J14 evidence](j14.json);
see the [generated image](j14.png). This is one sample, not a general guarantee
of constraint adherence. Image iteration returned a distinct image reference.
J12 succeeded on the third bounded run after two real search retry timeouts,
as recorded in the journey report. CI verification is still pending.

The conservative rewrite reservation was EUR0.020744. Each image call stayed
below the EUR0.05 combined reservation. No external image generator was used.
