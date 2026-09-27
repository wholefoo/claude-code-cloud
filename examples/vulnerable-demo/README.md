# Vulnerable demo (do not deploy)

A tiny FastAPI app with deliberately planted bugs, used to demo and test the RedBlue
security gate. It is never started by the gate on anything but a loopback preview.

| Bug | Where | Expected finding |
|---|---|---|
| SQL injection via f-string | `app.py` `/search` | `RB-SQLI` (auto-fix + regression test) |
| Reflected input without encoding | `app.py` `/search` | `RB-XSS-HTMLRESPONSE`, `RB-DAST-REFLECTED` |
| Open redirect | `app.py` `/go` | `RB-OPEN-REDIRECT`, `RB-DAST-OPEN-REDIRECT` |
| Unsafe YAML load | `app.py` `/import` | `RB-YAML-LOAD` (auto-fix + regression test) |
| Hardcoded secret | `app.py` | `RB-SECRET` |
| `|safe` on user content | `templates/comments.html` | `RB-JINJA-SAFE` (auto-fix) |
| No security headers | every page | `RB-DAST-CSP`, `RB-DAST-NOSNIFF`, … |

```bash
cd examples/vulnerable-demo
redblue gate scan            # fails the gate
redblue gate fix --dry-run   # writes verified fixes + regression tests to .redblue/fixes
```
