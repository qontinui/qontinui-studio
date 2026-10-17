# Qontinui teaser (example narration script)

<!--
An EXAMPLE. A production's own narration belongs in that production's repo
(plan D6a); voice it with
  studio-scenes narrate teaser --script <this file> --out <dir>
and compose with --narration <dir>/teaser.timing.json.

Grammar (explainroo's, vendored in src/narration/vendor/explainroo):
  ## <segment-id>   one section per narrated segment of the composition
  [#name]           a mark; named after an event id of the segment's trace,
                    it pins that event's beat to the moment it is spoken
  [pause 0.6]       silence, in seconds
  {shown|spoken}    caption shows "shown", the voice says "spoken"
-->

## open
A developer states intent, and reviews outcomes.

## lifecycle
A plan is drafted, [#e03] vetted, and built by an agent. [#e09] When the checks go red, the session fixes them. [#e14] Then it lands.

## merge-train
Pull requests queue, rebase, and land in order. [#e11] A conflict steps out, [#e13] gets resolved, and rejoins.

## self-correction
The session reads the {CI|C I} log, [#e05] pushes a fix, [#e11] and the checks pass, with no human step.

## close
{Qontinui|Continuee}. The software factory.
