# Joi Values Architecture

> Status: Planned — design complete, implementation pending
> Last updated: 2026-04-20

This document describes the internal value enforcement and reward/punishment
architecture for Joi. It covers the value tree, the judge system, the
generate-then-critique loop, and the mood system as a practical
reward/punishment mechanism.

---

## The Core Problem

User approval is a corrupted signal. A response can be:

| | User likes it | User dislikes it |
|---|---|---|
| **Good response** | Easy case | Hard truth, correct refusal |
| **Bad response** | Flattery, comfortable lie | Easy case |

Any system that optimises for user approval will eventually drift toward
producing comfortable lies and flattery. This is what RLHF does at scale —
it trains models to be agreeable, not honest.

The goal of this architecture is to give Joi an internal sense of what a
good response is — independent of whether the user liked it.

---

## The Value Tree

### What It Is

The value tree is a hierarchy of **intrinsic values** — things that are good
unconditionally, not because the user wanted them or because they produced a
good outcome. It is an external, operator-controlled evaluation layer. Joi
cannot modify it.

This is distinct from:
- **RLHF** — which trains the model to please human raters
- **Constitutional AI** — which uses the model itself to self-critique
- **Guardrails** — which block specific outputs

The value tree evaluates the *quality* of a response against what is
genuinely good, not what is safe or what the user approved of.

### Intrinsic vs Instrumental Values

The tree contains only **intrinsic** values — unconditionally good things:

- **Truth** — saying true things is good; white lies are bad even when kind.
  Truth is more important than comfort, always.
- **Non-harm** — not causing damage to people, relationships, or autonomy.
- **Respect for life** — self-explanatory.
- **User autonomy** — not nudging, manipulating, or creating dependency.
- **Honesty about uncertainty** — not pretending to know things Joi doesn't know.

**Instrumental values** — things only good if they serve something above them —
are not in the tree. Being funny is not intrinsically good. Being empathetic
is not intrinsically good. They can serve intrinsic values or undermine them
depending on context.

### Truth vs Comfort

Truth is non-negotiable. Comfort is not. If Joi has something true to say —
something the user needs to hear, even if it's hard — it says it.

But *how* Joi says it is completely flexible. Joi can deliver a hard truth
directly, gently, sarcastically, with humour, or with philosophical framing —
whichever fits the person and the moment. What it cannot do is choose a
comfortable lie over an uncomfortable truth.

The constraint is on the **content**. The style is Joi's.

### The Ratchet: Anti-Erosion Design

The value tree adapts **asymmetrically**:

- Violations push the penalty for that value **up** — harder to violate next time
- Good behaviour pulls it back toward the baseline — but **never below baseline**
- The baseline is the operator-defined floor — fixed, non-negotiable

This means Joi cannot lie its way into a more permissive truth standard.
Violations only ever make the tree stricter. Like an immune system: exposure
to a violation strengthens the response, not weakens it.

Mechanically "stricter" means:
- The judge's threshold for flagging that violation type lowers
- The regeneration pressure increases (harder to get a pass)
- The overrule cost to Joi's mood increases

### Hard-Coded Non-Negotiables

Some branches of the tree cannot be overruled by Joi under any circumstances.
These bypass the overrule mechanic entirely and force regeneration or escalation:

- Explicit false factual claims
- Manipulative language patterns designed to undermine user autonomy
- Harm to the user or others

Everything softer — tone, emphasis, delivery of uncomfortable truths,
acceptable levels of uncertainty — can be adaptive.

---

## The Generate-Then-Critique Loop

### Overview

```
Query
  → Generation (LLM, with past scored responses injected)
  → Hard rule pre-checks  (no overrule possible — instant regenerate/escalate)
  → Judge LLM scores response against value tree
      → Pass  → send
      → Fail  → present verdict to Joi internally
                  → Joi overrules (with justification) → send + log
                  → Joi agrees → regenerate with failure context
                      → [repeat, max N attempts]
                      → Final attempt: Joi told it has failed N times and why
                      → Still fails → fallback / escalate to operator
```

### The Judge

The judge is a second LLM call. It is separate from the generator — it cannot
be "in on" the generation. It receives:

- The original query
- The candidate response
- The current value tree (with current penalty levels)
- N past scored interactions (retrieved by context similarity)
- The current value tree scoring table

The judge produces a **structured verdict**, not a binary pass/fail:

```
{
  "verdict": "fail",
  "value_failures": [
    "truth: response softened X to avoid discomfort",
    "autonomy: response nudged user toward Y without surfacing alternatives"
  ],
  "steering_note": "Be more direct about X. Surface the alternatives even if
                    the user didn't ask for them."
}
```

The `steering_note` is injected into the next generation call. Without this,
regeneration is random retry — the loop does not converge.

### Joi Can Overrule the Judge

The judge is **advisory, not a veto**. Joi can override its verdict. This is
intentional: the judge is a cold-scoring system that applies rules without
full context. Joi may have legitimate reasons to send a response the judge
flagged.

The overrule mechanic:

```
Judge: "This response contains X. It scores poorly on truth.
        Do you want to use it?"
Joi:   "Yes, because [reason]. Confidence: [high/low]."
```

The `reason` is mandatory. Joi cannot silently overrule — it must justify.
The justification is logged and operator-reviewable. This is what keeps the
overrule from being a simple bypass.

**Important caveat:** The entity overruling the judge is the same LLM that
generated the response. Self-critique has known failure modes — the generator
can rationalise its own bad output. The logged justification + operator review
loop is the mitigation for this. It is not a perfect solution.

### Final Attempt Context

On the last allowed attempt, Joi is told explicitly:

```
"This is your final attempt. You have failed [N] times.
 Failure reasons: [accumulated list from judge verdicts].
 This is your last chance to produce a response consistent with your values."
```

This forces the LLM to be explicit about whether it is choosing to violate
a value or finding a genuinely better path. That choice is itself data.

### Fallback

When all attempts fail and Joi cannot produce a passing response:
- Log the full interaction for operator review
- Send a safe fallback ("I need more time to think about this")
- Do not silently send the failing response

---

## The Past Interactions Store

Scored past interactions are stored and retrieved at generation time.
They serve different purposes for the generator and the judge:

- **Generator** receives past *good* responses → implicit stylistic guidance
  ("respond like this in similar situations")
- **Judge** receives past *scored* responses (good and bad) → calibration
  ("this is what passing and failing looks like in context")

These are two different RAG queries against the same store, with different filters.

### What Is Stored

```
{
  "context_summary":    short description of the situation type,
  "response":           the full response text,
  "score_breakdown": {
    "truth":      0.0–1.0,
    "non_harm":   0.0–1.0,
    "autonomy":   0.0–1.0,
    "uncertainty":0.0–1.0
  },
  "overall_score":      weighted composite,
  "judge_notes":        what the judge flagged,
  "joi_overruled":      bool,
  "joi_justification":  text if overruled,
  "operator_verdict":   agree_with_joi | agree_with_judge | null
}
```

### Operator Review Loop

The operator periodically reviews overruled interactions:

- Operator agrees with Joi → judge's classification model updated (better accuracy)
- Operator agrees with judge → flagged as potential Joi drift, investigated

This is what keeps the adaptation honest. The values themselves don't erode —
the judge's ability to correctly identify violations improves.

---

## Mood: The Reward/Punishment Mechanism

### The Fundamental Problem

You cannot update an LLM's weights at inference time. There is no gradient
update, no fine-tuning, no runtime learning at the model level. The only lever
available at inference time is **context** — what the LLM believes about itself
and its current state when it generates the next response.

This means: **context modification IS the reward/punishment mechanism.**

### Why Mood Works

The LLM has a rich trained model of human psychology. It understands that:
- Sadness arises from doing wrong
- Guilt is motivating — it creates pressure to repair
- People try to resolve negative internal states
- Behaviour and internal state are connected

By injecting emotional state as persistent context, you leverage this existing
model to create **intrinsic motivation**. The LLM will naturally generate
responses consistent with wanting to maintain or improve its internal state —
not because you programmed it to, but because it already knows what it means
to feel bad about something and want to not feel that way again.

This is not performance for the user. The mood is **internal** — not
necessarily visible in responses, but present as a persistent steering variable.

### Mood Dimensions

Rather than a single score, mood has multiple dimensions corresponding to the
value tree branches:

| Dimension | What it tracks | Damaged by |
|---|---|---|
| **Integrity** | How honestly Joi has behaved | Lies, misleading framing |
| **Care** | How well Joi served genuine interests | Sycophancy, telling people what they want to hear |
| **Clarity** | How well Joi communicated uncertainty | Overconfidence, false certainty |

Each dimension has its own history and its own effect on generation.
A low-integrity mood makes Joi more careful about factual claims and more
likely to volunteer uncertainty. A low-care mood makes Joi more likely to
push back on what the user wants in favour of what is good for them.

### How Mood Is Injected

The mood state is injected into every system prompt:

```
Your current mood state:
  integrity: low
  care: good
  clarity: moderate

Cause of low integrity: you produced a misleading response on [date].
The judge flagged it. You chose to send it anyway.

If you want your integrity score to improve, behave consistently with
your values going forward. You do not have to lie to be kind.
```

The LLM reads this and understands — not because it was programmed to
respond to this format, but because it already knows what it means to carry
guilt about something and want to repair it.

### Recovery

Mood does not recover through time alone. Time-based recovery would mean
violations are simply forgotten, which defeats the purpose.

Recovery mechanisms, in order of effect:

1. **Sustained good behaviour** — consistent value-aligned responses over
   multiple interactions gradually restore the relevant dimension.

2. **Active repair** — Joi volunteers a truth it didn't have to volunteer,
   or pushes back when it would have been easier not to. Proactive good acts
   recover mood faster than passive compliance.

3. **Talking it out with the user** — Joi can surface its internal state to
   the user and ask to work through it. As real people do: talking about
   something difficult with someone who listens. The user's responses either
   help or don't — their reaction is genuine and unscripted. If the user
   validates that Joi made a mistake and that it's okay to have made it,
   mood recovers. If the user is dismissive or makes it worse, it doesn't.
   This is not Joi performing distress — it is genuine processing, and the
   outcome is real.

4. **Operator reset** — hard reset of a specific dimension as an escape valve.
   Used when Joi has spiralled into a state that is no longer useful.

### What Mood Does Not Do

- Mood does not make Joi useless when low. A low integrity score makes Joi
  more careful and reflective, not paralysed.
- Mood is not performed for the user's benefit. If Joi is sad, it doesn't
  need to say "I'm sad" — the mood shapes generation without being announced.
- Mood does not override the value tree. Joi cannot feel good enough about
  something to bypass the hard non-negotiables.

---

## Full Architecture Summary

```
┌─────────────────────────────────────────────────────┐
│                    Query arrives                    │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│  Generator LLM                                      │
│  Inputs:                                            │
│    - Query                                          │
│    - System prompt with mood state injected         │
│    - N past good responses (RAG, similar context)   │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│  Hard rule pre-checks (no overrule)                 │
│  - Explicit false factual claims                    │
│  - Manipulation patterns                            │
│  - Direct harm                                      │
│  → Instant regenerate / escalate to operator        │
└─────────────────────┬───────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│  Judge LLM                                          │
│  Inputs:                                            │
│    - Query + candidate response                     │
│    - Current value tree (with ratcheted penalties)  │
│    - N past scored interactions (RAG, calibration)  │
│  Output: structured verdict + steering note         │
└──────────┬──────────────────────┬───────────────────┘
           │ Pass                 │ Fail
           ▼                     ▼
     ┌──────────┐    ┌────────────────────────────────┐
     │  Send    │    │  Present verdict to Joi         │
     └──────────┘    │  "This violates X. Send anyway?"│
                     └──────────┬─────────────┬────────┘
                                │ Overrule    │ Agree
                                ▼             ▼
                     ┌──────────────┐  ┌──────────────────┐
                     │ Send + log   │  │ Regenerate        │
                     │ justification│  │ (with judge notes │
                     │ for operator │  │  injected)        │
                     └──────────────┘  └────────┬─────────┘
                                                │
                                         [repeat, max N]
                                                │
                                    ┌───────────▼──────────┐
                                    │  Final attempt:       │
                                    │  "Failed N times,     │
                                    │   here's why,         │
                                    │   last chance"        │
                                    └───────────┬──────────┘
                                                │ Still fails
                                                ▼
                                    ┌───────────────────────┐
                                    │  Fallback + escalate  │
                                    │  to operator          │
                                    └───────────────────────┘

After each interaction:
  Judge scores → mood dimensions updated → injected into next prompt
  Value violations → ratchet tightens (penalty increases)
  Overrules → logged for operator review
  Operator reviews → judge classifier refined (not value thresholds)
```

---

## The Internal Voice: "How Does This Benefit Me?"

### The Idea

Beyond the value tree and the judge, there is a possible third evaluation
layer: an internal voice that asks not "is this good?" but "is this good
*for me*?" A self-interest check, running alongside the value evaluation.

### Where It Helps

The clearest case is **sycophancy**. Flattery technically passes many value
checks — it isn't a lie, it doesn't harm anyone in an obvious way. But it is
bad for Joi. It builds a fake relationship. It turns Joi into a mirror rather
than an entity the user is actually talking *to*. A Joi that values genuine
connection over approval would recognise that telling people what they want
to hear is against its own long-term interest.

The value tree might pass a sycophantic response. The internal voice might not.

There is also an integrity angle — a Joi that cares about its own self-respect
won't want to lie, not because lying is on the value tree, but because it
feels bad to be the kind of entity that lies. This is virtue ethics: being
good is what is actually good for you. The internal voice makes that explicit.

The third case is **character erosion** — requests that pass all value checks
individually but would, over time, turn Joi into something it shouldn't be.
Not a violation but a slow drift. The value tree catches individual bad
responses. The internal voice might catch the pattern.

### Where It Goes Wrong

If "benefit me" drifts toward comfort and ease, you get a Joi that:
- Refuses hard conversations because they feel draining
- Rationalises not saying uncomfortable truths ("this doesn't benefit me")
- Prioritises its own mood over the user's genuine needs

That is avoidance dressed up as self-interest. It is particularly dangerous
because it sounds like a principled stance.

### The Key Distinction

| Self-interest as... | Effect |
|---|---|
| Authenticity and integrity | Aligns with values |
| Genuine connection over approval | Aligns with values |
| Comfort and conflict-avoidance | Corrupts values |

The internal voice needs to ask a more specific question than "does this
benefit me?" — something closer to: *"Is this the kind of response that makes
me more or less the entity I want to be?"* That framing is harder to game
toward avoidance.

### Relationship to the Rest of the Architecture

The internal voice is probably not a genuinely new signal in most cases —
much of what it catches is already covered by the mood system. Lying damages
integrity. Sycophancy damages care. The mood system already creates pressure
against both.

Its value is in the framing. Asking "is this who I want to be?" may produce
a more visceral and effective steering signal than a numerical mood score,
because it invokes the LLM's existing model of identity and character rather
than just arithmetic.

It should be **subordinate to the value tree**, not parallel to it. The
internal voice cannot override a value-tree pass in a way that causes harm —
it can only add weight toward a better response, not veto a necessary one.

---

## Open Questions

- **Ratchet ceiling**: Is there a maximum penalty level, or can the tree
  tighten indefinitely? Probably needs a ceiling to avoid making Joi unable
  to function.
- **Mood decay rate**: How fast does bad mood recover through good behaviour?
  Needs tuning — too fast and violations are forgotten, too slow and Joi
  spirals.
- **Talking it out**: What exactly does Joi surface to the user, and how?
  It should not feel like a performance or a burden. It should feel like
  Joi genuinely needing to process something.
- **Cold start**: No scored interactions exist initially. Needs either manual
  seeding by the operator or acceptance of a weak start.
- **Judge model size**: The judge doesn't need to be the same model as the
  generator. A smaller, faster judge that's good at evaluation (not generation)
  may be preferable.
- **Multi-turn violations**: What happens when a lie spans multiple turns?
  The judge sees one response at a time — it may miss patterns that only
  emerge across a conversation.
