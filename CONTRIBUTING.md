# Contributing

Thanks for improving `zcode-session-exporter`.

## Development setup

```bash
git clone https://github.com/Dongmingyue-loss/zcode-session-exporter.git
cd zcode-session-exporter
python -m unittest discover -s test -v
```

Runtime code uses only the Python standard library. Development/CI extras:

```bash
python -m pip install -r requirements-dev.txt
```

## Before opening a pull request

1. Run the unit and API tests:

   ```bash
   python -m unittest discover -s test -v
   ```

2. Run the syntax/undefined-name checks:

   ```bash
   ruff check --select E9,F .
   ```

3. For UI changes, run the Playwright smoke test:

   ```bash
   python scripts/make_demo_db.py
   python test/ui_smoke.py
   ```

4. Do not commit real session databases, exported conversations, API keys, or screenshots containing real data. Use `demo/demo.sqlite` and synthetic fixtures only.

## Pull request expectations

- Keep changes focused.
- Add or update tests for behavior changes.
- Update `README.md` / `docs/` for user-visible changes.
- Preserve the read-only, local-only security model.
