# Context for AI assistants

This folder exists so that an assistant starting a new session can understand
the project quickly without re-deriving it from the code or from old chats.
The root [`CLAUDE.md`](../../CLAUDE.md) is the short entry point; these files
hold the detail.

| File | Read it when |
|---|---|
| [`project-brief.md`](project-brief.md) | You need the goal, scope, constraints and the owner's decisions |
| [`architecture.md`](architecture.md) | You are about to change backend or frontend code, especially the copy engine |
| [`glossary.md`](glossary.md) | A trading or project term is unclear (NQ, bracket, target, reconcile ...) |
| [`status-and-roadmap.md`](status-and-roadmap.md) | You need to know what is done, what is next and what is known to be missing |
| [`working-agreement.md`](working-agreement.md) | You are about to talk to the owner, deliver work or use git |
| [`live-brokers.md`](live-brokers.md) | The owner asks about connecting real brokers |

Rules for keeping this folder useful:

* Facts only. If something is a guess or was not verified, say so.
* Update `status-and-roadmap.md` at the end of every milestone.
* When a design decision changes, change `architecture.md` in the same commit
  as the code.
* No secrets, credentials or personal data here. This repository may be public.
