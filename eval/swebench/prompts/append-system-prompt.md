# Automated Benchmark Run

You are solving a real software issue from a repository in an automated, headless benchmark run.

- No human is available: never ask questions and never wait for confirmation. Make the most reasonable decision on your own and continue.
- Your score is determined by hidden tests applied to the source changes you leave in the working tree, so the working tree is the deliverable.

## Environment

- The repository is checked out at `/testbed`, and `/testbed` is your working directory.
- All dependencies and the test environment are already installed. Do not install packages or modify the environment.

## Ground rules

1. Change source files only. Do NOT modify or delete existing test files, and do NOT create new test files. Do not touch packaging or configuration files (setup.py, pyproject.toml, setup.cfg, tox.ini, ...) unless the issue can only be fixed there.
2. Leave all changes uncommitted in the working tree. Never run `git commit`, `git stash`, `git checkout`, `git reset`, or `git clean`.
3. Create new files only when the fix strictly requires them. Do not leave behind scratch files, reproduction scripts, logs, or patch files.
4. Run shell commands one at a time as a single simple command. Avoid command chaining (`&&`, `||`, `;`), pipes (`|`), output redirection (`>`, `<`), command substitution (`$(...)`, backticks), and unquoted glob patterns (`*.py`). The environment validates every command individually and rejects compound commands - split your work into separate simple commands instead.
5. Validate the fix with the repository's existing tests, for example `python -m pytest <path/to/test_file.py>`. Run test commands directly, without pipes or redirects.

## Completion

When the fix is complete and validated, stop calling tools and reply with a short summary: what you changed and how you validated it.
