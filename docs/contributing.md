# Contributing to Plumbus Code

Thank you for contributing to **Plumbus Code**.

Plumbus Code is an open-source terminal coding agent. The project is still actively evolving, so contributions should prioritize clear architecture, small reviewable changes, and keeping the codebase understandable as the project grows.

Please read this guide before opening a pull request.

---

## Development Philosophy

Plumbus Code is being built as a modular terminal agent.

When contributing, prefer:

* Small, focused changes
* Clear architectural boundaries
* Simple implementations over unnecessary abstractions
* Code that is easy to understand and modify
* Changes that solve one problem at a time

Avoid introducing abstractions or dependencies unless they provide a clear benefit to the project.

Before making a large architectural change, open an issue or discussion first so the approach can be agreed upon before significant implementation work begins.

The architecture and responsibilities of the major components are documented in [`docs/`](./docs).

---

# Branching Strategy

`main` is a protected branch.

**Nobody commits directly to `main`. This includes project maintainers.**

Every change must be developed on a separate branch and merged through a pull request.

The basic workflow is:

```text
main
  │
  ├── feature/add-tool-loop
  │
  ├── fix/resume-state
  │
  ├── refactor/llm-interface
  │
  └── docs/contributing-guide
```

Once a pull request is reviewed and approved, the branch can be merged into `main`.

### Branch naming

Use descriptive branch names:

```text
feature/<description>
fix/<description>
refactor/<description>
docs/<description>
test/<description>
chore/<description>
```

Examples:

```text
feature/add-shell-tool
fix/tool-timeout
refactor/agent-loop
docs/update-architecture
test/add-agent-loop-tests
chore/update-dependencies
```

Keep branch names short and descriptive.

---

# Pull Requests

**All changes must go through a pull request.**

The expected workflow is:

```text
1. Update from main
       ↓
2. Create a branch
       ↓
3. Make the change
       ↓
4. Run the relevant checks
       ↓
5. Commit the change
       ↓
6. Push the branch
       ↓
7. Open a pull request
       ↓
8. Review
       ↓
9. Merge into main
```

Do not push changes directly to `main`.

This applies to:

* Features
* Bug fixes
* Refactors
* Documentation
* Tests
* Dependency changes
* Configuration changes

Even small changes should use the same workflow. This keeps the project history consistent and makes it possible to review every change.

---

# Commit Convention

Plumbus Code uses **Conventional Commits**.

The general format is:

```text
<type>: <description>
```

Examples:

```text
feat: add shell tool
fix: resume interrupted runs
refactor: separate agent and model interfaces
test: add tool execution tests
docs: document agent architecture
chore: update dependencies
```

## Commit Types

### `feat`

A new feature or capability.

```text
feat: add file search tool
```

### `fix`

A bug fix.

```text
fix: prevent duplicate tool execution
```

### `refactor`

A code change that does not change intended behavior.

```text
refactor: simplify agent loop
```

### `test`

Adding or modifying tests.

```text
test: add agent state tests
```

### `docs`

Documentation-only changes.

```text
docs: explain tool architecture
```

### `chore`

Maintenance changes that do not directly affect application behavior.

```text
chore: update dependencies
```

---

# Atomic Commits

Commits should represent **one logical change**.

Good:

```text
feat: add shell tool
```

where the implementation, required supporting changes, and relevant documentation all belong to that feature.

Avoid unrelated changes in the same commit.

Bad:

```text
feat: add shell tool and refactor llm client and update README
```

if those are actually three independent changes.

Prefer:

```text
feat: add shell tool
refactor: simplify llm client
docs: update readme
```

Atomic commits make the history easier to understand, review, revert, and debug.

---

# Do Not Commit Every Tiny Edit

Atomic does **not** mean committing after every file modification.

For example, avoid:

```text
feat: add shell tool
feat: add shell tool exception
feat: add shell tool import
feat: add shell tool documentation
```

Instead, combine the work into one coherent change:

```text
feat: add shell tool
```

A commit should answer:

> **What meaningful change does this commit introduce?**

---

# Keep Commits Focused

Do not mix unrelated work.

For example, if you are implementing a new tool and notice an unrelated bug in the LLM client, don't silently include the LLM fix in the same feature commit.

Instead:

```text
feature/add-shell-tool
```

should contain the shell-tool work.

The unrelated fix should become its own branch:

```text
fix/llm-client-error
```

with its own pull request.

If the unrelated issue is necessary to complete the feature, explain the relationship clearly in the pull request.

---

# Architecture

Before contributing code, understand the architecture documented in:

```text
docs/
```

Plumbus Code is intentionally divided into separate responsibilities.

As a general rule:

```text
agent/
    Agent orchestration and execution logic

ai/
    LLM interfaces and model providers

tools/
    Tool implementations

tests/
    Tests

docs/
    Architecture and project documentation
```

Keep responsibilities within their appropriate boundaries.

For example, tool implementations should not become responsible for controlling the agent's execution loop, and model-provider code should not contain terminal-agent orchestration logic.

When adding new functionality, first ask:

> **Which component owns this responsibility?**

If the answer is unclear, check the architecture documentation or open a discussion before implementing it.

---

# Making Changes to the Architecture

Architectural changes require additional care.

Before implementing a significant architectural change:

1. Read the relevant documentation in `docs/`.
2. Open an issue or discussion describing the problem.
3. Explain the proposed approach.
4. Discuss alternatives if appropriate.
5. Wait for the approach to be agreed upon before implementing a large change.

Avoid introducing a new abstraction simply because it makes the immediate implementation easier.

The goal is to keep Plumbus Code understandable as the project grows.

---

# Pull Request Guidelines

A pull request should be focused on one logical change.

A good PR should explain:

### What changed?

Briefly describe the implementation.

### Why?

Explain the problem the change solves.

### How?

Explain any important implementation or architectural decisions.

### Additional considerations

Mention anything reviewers should pay particular attention to.

For example:

```text
## What

Adds a shell execution tool to the agent.

## Why

The agent currently has no way to execute shell commands.

## How

Introduces the shell tool through the existing tool interface without
adding execution logic to the agent loop.

## Notes

Command execution behavior and error handling are documented separately.
```

---

# Before Opening a Pull Request

Before opening a PR:

* Make sure your branch is based on the current `main`.
* Review your own diff.
* Remove unrelated changes.
* Make sure commit messages follow Conventional Commits.
* Run the checks currently available in the project.
* Update relevant documentation when behavior or architecture changes.
* Make sure the PR description explains the change.

As the project develops, additional required checks and testing requirements will be documented here.

---

# Keeping Your Branch Up to Date

Before opening or merging a PR, make sure your branch is up to date with `main`.

For example:

```bash
git fetch origin
git rebase origin/main
```

Resolve any conflicts locally, then push the updated branch.

If the branch has already been pushed and rebased, use:

```bash
git push --force-with-lease
```

Do **not** use:

```bash
git push --force
```

`--force-with-lease` helps prevent accidentally overwriting someone else's changes.

---

# Recommended Git Workflow

A typical contribution should look like:

```bash
git checkout main
git pull origin main

git checkout -b feature/my-feature

# Make changes

git status
git diff

# Run available checks

git add <files>
git commit -m "feat: add my feature"

git push -u origin feature/my-feature
```

Then open a pull request against:

```text
main
```

After review and approval, the PR can be merged.

---

# What Not To Do

Please avoid:

### Direct commits to `main`

```bash
git checkout main
git commit ...
git push origin main
```

All changes must go through a PR.

### Giant commits

Avoid commits containing unrelated features, refactors, documentation, and fixes.

### Unnecessary dependencies

Don't add a dependency when the same functionality can reasonably be implemented with the existing stack.

### Architecture violations

Don't bypass established interfaces simply because doing so is faster.

### Drive-by refactors

Don't substantially rewrite unrelated code while implementing a feature.

If a refactor is valuable, make it a separate change.

### Generated or local files

Do not commit local development artifacts such as:

```text
__pycache__/
*.pyc
.env
virtual environments
IDE configuration
local databases
```

unless a particular generated file is explicitly required by the project.

---

# When in Doubt

If you're unsure about:

* Where code belongs
* Whether an abstraction is appropriate
* Whether something should be a new feature or refactor
* Whether an architectural change is too large
* How a component should interact with another component

**Open an issue or discussion before implementing a large change.**

It is much easier to agree on the design before implementation than to redesign a large pull request afterward.

---

# Contribution Principle

The goal is not simply to add more code.

The goal is to make Plumbus Code **better, simpler, and easier to build on**.

Prefer changes that leave the project in a state where the next contributor can understand what you did and why.
