# TAP rule naming convention

A naming convention for Fireblocks Transaction Authorization Policy rules in a **shared demo workspace** — one
used by many sales engineers, and shown on screen to prospects. It is not a security standard; it is a legibility
standard. The goal is that anyone can open any policy type and tell, at a glance, what each rule is for and
whether it is safe to touch.

Rule names in a demo workspace are customer-visible. They should read like a real customer's policy, not like
internal shorthand.

## The grammar

```
[Scope] Subject (qualifier)
```

**`[Scope]`** — required. From a fixed vocabulary, 14 characters or fewer including the brackets. Names the demo,
integration or function the rule serves. The *same vocabulary is used in every policy type*, which is what makes a
TRANSFER rule and an APPROVE rule recognisably part of one system.

**Subject** — required. Two to five words, sentence case, naming the business action. For a catch-all the action is
the policy type itself: `Catch-all transfer`, `Catch-all mint`, `Catch-all program call`.

**`(qualifier)`** — optional. Use it only when two rules in the same scope would otherwise read identically. It
carries the single condition that distinguishes them: an amount band, a destination type, an asset.

## Rules

1. **Never restate what the rule's own columns show.** Initiator, signer, asset, source, destination, amount and
   action each have a column in the console. `(1 approval)` and `(signer: Everyone)` spend name width on
   information already on screen. Once subjects are distinct, most qualifiers stop earning their place.
2. **BLOCK rules begin the subject with "Block".** The one verdict worth stating in the name, because a block is a
   deliberate statement rather than a default.
3. **No order numbers in names.** The console numbers the rows. Hand-written numbers go stale the moment anyone
   reorders, and a stale number is worse than none.
4. **A scope tag must cover at least two rules workspace-wide.** Otherwise use the nearest broader tag. This is the
   rule that stops the vocabulary drifting into one tag per rule.
5. **Budget 48 characters.** Not an API limit — `name` is an unconstrained string and far longer names are
   accepted. It is a truncation limit for the console's Name column.
6. **Nothing ships named `Edit to rename`.** That is the Fireblocks placeholder. A rule carrying it has never been
   reviewed by a human.

## Scope vocabulary

Keep the list short and shared across every policy type. The reserved tags:

| Tag | Meaning |
|---|---|
| `[Core]` | **Only** the baseline rules at the bottom of a policy: the catch-all and the terminal deny. Nothing else. |
| `[Guardrail]` | A deliberate restriction sitting *above* the baseline — a block, an approval requirement, a ceiling. |
| `[Shared]` | Available to every user in the workspace. |
| `[Sandbox]` | One person's rule. Tagging it honestly makes it a visible cleanup candidate. |

Everything else is a demo or integration name (`[Swap]`, `[Gasless]`, `[AML]`, and whichever products the
workspace actually demonstrates). Add one only when it will cover two or more rules.

## Ordering

TAP is first-match-wins, so order is semantics, not presentation. The convention makes the order self-checking:

```
scope-specific rules  →  [Guardrail]  →  [Core]
```

`[Core]` anywhere but the bottom means something is wrong. If a rule genuinely has to sit below the catch-all,
that is worth a comment in the review, because it is almost certainly dead.

## Run the consolidator before naming anything

The most common defect this convention surfaced was not a bad name. It was **a permissive catch-all placed above
the guardrails it was supposed to sit beneath**, silently making every rule below it unreachable. It appeared in
four separate policy types in one workspace, including a wildcard `ALLOW` whose name described a specific DeFi
integration, and a default-deny that had never once fired.

Naming a dead rule is wasted effort, and a tidy name on a rule that cannot fire is worse than an untidy one — it
implies a protection that does not exist. So:

```
tap_consolidate.consolidate(rules, default_deny=False)
```

Let the consolidator decide which rules survive, then name the survivors. It verifies equivalence cell by cell
rather than pattern-matching, so every removal it proposes is proved rather than guessed.

**It cannot model every rule shape.** Rules using `dAppAddress`, `account`, `programCall` or an unusual
`destination` selector come back marked *opaque* and are left in place. Opaque is not a clean bill of health; it
means "unproven". Treat those policy types as rename-only unless someone who knows the demos reviews a merge by
hand.

## Watch for names that contradict their configuration

Separately from dead rules, check that each name matches what the rule actually does. Real examples found in one
workspace, all of which had been live for months:

- a rule named after a DeFi protocol whose conditions were all wildcards — a blanket allow-everything
- a rule named "Repayment" that moved funds in the direction of a collateral *release*
- a rule named "EVM Only" whose `platform` field was `*`
- a rule whose name implied it was a wildcard but which was correctly scoped (the name was pessimistic, not the config)
- a correctly-aimed block whose whitelist entry carried a misleading display label from a different protocol

A name that disagrees with its configuration is a latent incident. Rename it to what it does, or fix the
configuration to match the name — but never leave the two disagreeing.

## Don't collapse rules that earn their separation

Fewer rules is not the goal; clarity is. In a policy where each rule names one integration — a dApp-connection
policy, typically — merging them into a single multi-domain rule cuts the count and destroys the thing that made
the policy readable. A prospect scrolling that screen should see the integrations by name.

Merge when rules are redundant. Keep them when each one is telling the reader something.
