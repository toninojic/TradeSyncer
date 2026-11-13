# Working with the owner

## Communication

* Speak **Serbian (Latin script)** with the owner. Everything in the repository
  stays in English: code, comments, UI text, docs, commit messages.
* Explain in plain, normal language. The owner is not a professional developer
  and asked for simple explanations, but explicitly does **not** want a
  childish tone or analogies. Define a term the first time it is used.
* Be concrete: exact commands to copy, what should appear, what to click.
* Say plainly what was not verified or does not work yet.

## The owner's environment

* Windows PC, **Git Bash (MINGW64)** as the terminal. PowerShell commands do
  not work there: use forward slashes (`/e/...`) and
  `source .venv/Scripts/activate`.
* Python **3.13** (`py -3.13`); the project supports 3.12+.
* The virtual environment is `backend/.venv`.
* Node.js is not required to run the app (the built panel is committed).
* `getpass` hangs in Git Bash's terminal, so `app.cli` falls back to a visible
  prompt there.

## Process

1. Plan first for anything non-trivial, and ask before coding when a decision
   belongs to the owner (trading behaviour, risk semantics, anything about real
   money).
2. Build in milestones. Stop after each one, run the tests, and tell the owner
   how to start and try it.
3. Verify before reporting: `pytest -q`, `npm run build`, and for UI changes an
   actual browser check.
4. Report bugs that tests or manual checks uncovered, including the ones that
   were fixed along the way.

## Git

* Remote: `origin` -> `https://github.com/toninojic/TradeSyncer.git`, branch
  `main`. The repository can be read without logging in, so treat everything
  committed as public.
* The canonical working copy is the owner's local clone. Commits are made
  there with the owner's git identity; do not put a different author on them.
* Commit at the end of a milestone or a self-contained change. Subject line in
  the imperative ("Add risk rule enforcement"), a short body saying what and
  why.
* Before every commit run `git status` and check that none of these are
  staged: `.env`, `backend/data/`, `*.db`, `.venv/`, `node_modules/`.
* When `frontend/src` changes, rebuild and commit `frontend/dist` in the same
  commit.
* Do not rewrite published history (no force-push) unless the owner asks.
* If you build in a separate sandbox, start from a fresh clone and bring the
  changes back to the owner's working copy before committing, so there is only
  one line of history.

## Things not to do

* No real broker connections and no real orders in the MVP.
* Do not log, print or commit credentials. Broker credentials are write-only
  through the API.
* Do not change the server to listen on anything other than `127.0.0.1` by
  default.
* Do not delete files in the owner's folder unless asked.
