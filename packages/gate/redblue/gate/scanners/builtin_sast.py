"""Built-in AST rules for FastAPI / Jinja / SQLAlchemy apps.

Every rule has an id (``RB-*``), a CWE and a default severity. The analysis is local (one
function at a time) with a tiny amount of taint tracking: a local variable assigned from a
dynamic string counts as dynamic where it is used.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from redblue.gate.scanners import ScanContext, iter_files
from redblue.gate.schemas import Finding, Severity


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    cwe: str
    severity: Severity


RULES: dict[str, Rule] = {
    r.id: r
    for r in [
        Rule("RB-SQLI", "SQL query built from dynamic string", "CWE-89", Severity.high),
        Rule(
            "RB-CMDI", "OS command built from dynamic input / shell=True", "CWE-78", Severity.high
        ),
        Rule("RB-EVAL", "eval/exec of dynamic input", "CWE-95", Severity.high),
        Rule("RB-PICKLE", "Unsafe deserialization (pickle/marshal)", "CWE-502", Severity.high),
        Rule("RB-YAML-LOAD", "yaml.load without SafeLoader", "CWE-502", Severity.high),
        Rule("RB-TLS-VERIFY", "TLS certificate verification disabled", "CWE-295", Severity.medium),
        Rule("RB-DEBUG", "Debug mode enabled", "CWE-489", Severity.medium),
        Rule("RB-SECRET", "Hardcoded secret", "CWE-798", Severity.high),
        Rule("RB-OPEN-REDIRECT", "Redirect to request-controlled URL", "CWE-601", Severity.medium),
        Rule(
            "RB-XSS-HTMLRESPONSE",
            "HTMLResponse built from unescaped input",
            "CWE-79",
            Severity.high,
        ),
        Rule("RB-XSS-MARKUP", "Markup() wrapping dynamic content", "CWE-79", Severity.medium),
        Rule("RB-JINJA-SAFE", "`|safe` filter disables autoescaping", "CWE-79", Severity.medium),
        Rule("RB-JINJA-AUTOESCAPE-OFF", "Jinja autoescape disabled", "CWE-79", Severity.high),
        Rule("RB-CORS", "CORS allows any origin with credentials", "CWE-942", Severity.high),
        Rule(
            "RB-JWT-NOVERIFY",
            "JWT decoded without signature verification",
            "CWE-347",
            Severity.high,
        ),
        Rule("RB-WEAK-HASH", "Fast unsalted hash used for passwords", "CWE-916", Severity.high),
        Rule(
            "RB-WEAK-RANDOM",
            "Non-cryptographic random used for secrets",
            "CWE-338",
            Severity.medium,
        ),
        Rule(
            "RB-AUTHZ", "Possible missing authorization on object lookup", "CWE-639", Severity.info
        ),
    ]
}

SQL_KEYWORDS = re.compile(
    r"\b(select|insert|update|delete|where|from|into|values|order\s+by|drop|create|alter)\b",
    re.I,
)
SQL_SINKS = {"execute", "executemany", "exec_driver_sql", "executescript", "raw", "text", "mogrify"}
SECRET_NAME = re.compile(
    r"(secret|passw(or)?d|passwd|pwd|api_?key|apikey|auth_?token|access_?token|token|"
    r"private_?key|access_?key|auth_?key|client_?secret|credential)",
    re.I,
)
SECRET_NAME_EXCLUDE = re.compile(
    r"(_url|_uri|_field|_name|_type|_header|_path|_file|_env|_var|_len|_length|_prefix|"
    r"_pattern|_regex|_re|_hint|_label|_param|_endpoint|_expires?|_ttl|_age|_cookie|_policy|"
    r"_min|_max|_hash|_hasher|_form|_input|_placeholder)$|^(min|max|is|has|use|require)_",
    re.I,
)
PLACEHOLDER = re.compile(
    r"(changeme|change-me|example|your[-_]|xxx|placeholder|dummy|replace|redacted|<|>|\$\{|\{\{|"
    r"\*\*\*|not-?set|todo|test|fake)",
    re.I,
)
SECRETY_CONTEXT = re.compile(
    r"(token|secret|passw|nonce|salt|otp|csrf|session|api_?key|reset|invite|verif)", re.I
)
PASSWORD_CONTEXT = re.compile(r"(passw|pwd|passwd)", re.I)
USER_CONTENT_WORDS = re.compile(
    r"\b\w*(comment|body|content|message|text|bio|name|title|description|query|search|input|"
    r"user|author|note|review|website|url|link|q)\w*\b",
    re.I,
)
ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "route"}
AUTH_DEP_WORDS = re.compile(
    r"(user|auth|login|admin|permission|perm|require|role|principal|current|token|verify|staff|"
    r"owner|session|member|account|guard|protect)",
    re.I,
)
DB_CALLS = {
    "execute",
    "query",
    "scalar",
    "scalars",
    "scalar_one",
    "scalar_one_or_none",
    "filter_by",
    "get_one",
    "fetchone",
    "fetchall",
}
SANITIZER_WORDS = re.compile(
    r"(escape|safe|clean|sanitize|quote|bleach|validate|url_for|url_path_for)", re.I
)
REDIRECT_PARAM = re.compile(
    r"^(next|url|redirect|redirect_to|redirect_uri|redirect_url|return|return_to|return_url|"
    r"returnto|to|continue|dest|destination|goto|target|back|callback)$",
    re.I,
)


def dotted(node: ast.AST | None) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        inner = dotted(node.func)
        if inner:
            parts.append(inner + "()")
    else:
        return ""
    return ".".join(reversed(parts))


def _is_const_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id.isupper()


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_false(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def const_text(node: ast.AST) -> str:
    """Concatenated literal text of a string expression (for keyword checks)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(const_text(v) for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.BinOp):
        return const_text(node.left) + " " + const_text(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return const_text(node.func.value)
    return ""


def _escaped(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and bool(SANITIZER_WORDS.search(dotted(node.func) or ""))


def _concat_leaves(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _concat_leaves(node.left) + _concat_leaves(node.right)
    return [node]


def route_info(func: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, str] | None:
    """(METHOD, path) if ``func`` is decorated as a FastAPI/Starlette route."""
    for dec in func.decorator_list:
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        attr = dec.func.attr
        if attr not in ROUTE_METHODS:
            continue
        path = None
        if (
            dec.args
            and isinstance(dec.args[0], ast.Constant)
            and isinstance(dec.args[0].value, str)
        ):
            path = dec.args[0].value
        elif isinstance(_kw(dec, "path"), ast.Constant):
            path = _kw(dec, "path").value  # type: ignore[union-attr]
        if path is None:
            continue
        method = attr.upper() if attr not in {"api_route", "route"} else "GET"
        methods = _kw(dec, "methods")
        if (
            isinstance(methods, ast.List)
            and methods.elts
            and isinstance(methods.elts[0], ast.Constant)
        ):
            method = str(methods.elts[0].value).upper()
        return method, path
    return None


def enclosing_function(tree: ast.AST, lineno: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and (best is None or node.lineno >= best.lineno):
                best = node
    return best


class _Visitor(ast.NodeVisitor):
    def __init__(self, source: str, rel: str):
        self.source = source
        self.lines = source.splitlines()
        self.rel = rel
        self.findings: list[Finding] = []
        self.func_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.env_stack: list[dict[str, ast.expr]] = [{}]
        self.stmt_stack: list[ast.stmt] = []

    # ------------------------------------------------------------ plumbing

    def add(
        self,
        rule_id: str,
        node: ast.AST,
        detail: str = "",
        severity: Severity | None = None,
        confidence: str = "medium",
    ) -> None:
        rule = RULES[rule_id]
        line = getattr(node, "lineno", None)
        code = self.lines[line - 1].strip() if line and line <= len(self.lines) else ""
        fn = self.func_stack[-1].name if self.func_stack else "<module>"
        self.findings.append(
            Finding(
                tool="redblue-sast",
                rule_id=rule_id,
                title=rule.title,
                description=detail or rule.title,
                severity=severity or rule.severity,
                confidence=confidence,  # type: ignore[arg-type]
                cwe=rule.cwe,
                file=self.rel,
                line=line,
                evidence=f"in {fn}(): {code}"[:500],
                code=code,
            )
        )

    def visit(self, node: ast.AST):
        if isinstance(node, ast.stmt):
            self.stmt_stack.append(node)
            try:
                return super().visit(node)
            finally:
                self.stmt_stack.pop()
        return super().visit(node)

    @property
    def env(self) -> dict[str, ast.expr]:
        return self.env_stack[-1]

    def _stmt_text(self) -> str:
        if not self.stmt_stack:
            return ""
        return ast.get_source_segment(self.source, self.stmt_stack[-1]) or ""

    def resolve(self, node: ast.AST, depth: int = 0) -> ast.AST:
        if isinstance(node, ast.Name) and depth < 3 and node.id in self.env:
            return self.resolve(self.env[node.id], depth + 1)
        return node

    def is_dynamic_str(self, node: ast.AST, depth: int = 0) -> bool:
        node = self.resolve(node) if depth == 0 else node
        if isinstance(node, ast.JoinedStr):
            return any(
                isinstance(v, ast.FormattedValue) and not _is_const_name(v.value)
                for v in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = [node.left, node.right]
            has_str = any(const_text(p) for p in parts)
            has_dyn = any(
                not isinstance(p, ast.Constant)
                and not _is_const_name(p)
                and (
                    not isinstance(p, ast.BinOp | ast.JoinedStr)
                    or self.is_dynamic_str(p, depth + 1)
                )
                for p in parts
            )
            return has_str and has_dyn
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return bool(const_text(node.left)) and not isinstance(node.right, ast.Constant)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Constant)
        ):
            return bool(node.args or node.keywords)
        return False

    def _params(self) -> set[str]:
        if not self.func_stack:
            return set()
        a = self.func_stack[-1].args
        return {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}

    def request_derived(self, node: ast.AST, depth: int = 0) -> bool:
        if depth > 3:
            return False
        if _escaped(node):
            return False
        if isinstance(node, ast.Name):
            if node.id in self.env:
                return self.request_derived(self.env[node.id], depth + 1)
            return node.id in self._params() and node.id not in {"self", "cls", "request"}
        text = ast.get_source_segment(self.source, node) or ""
        if re.search(r"request\.(query_params|headers|form|args|values|cookies|path_params)", text):
            return True
        if isinstance(node, ast.JoinedStr):
            return any(
                isinstance(v, ast.FormattedValue) and self.request_derived(v.value, depth + 1)
                for v in node.values
            )
        if isinstance(node, ast.BinOp):
            return self.request_derived(node.left, depth + 1) or self.request_derived(
                node.right, depth + 1
            )
        if isinstance(node, ast.Call) and not _escaped(node):
            return any(self.request_derived(a, depth + 1) for a in node.args)
        return False

    # ------------------------------------------------------------ scopes

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.func_stack.append(node)
        self.env_stack.append({})
        self._check_authz(node)
        self.generic_visit(node)
        self.env_stack.pop()
        self.func_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.env[target.id] = node.value
            self._check_secret(target, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            if isinstance(node.target, ast.Name):
                self.env[node.target.id] = node.value
            self._check_secret(node.target, node.value, node)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values, strict=False):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                self._check_secret_name(k.value, v, node)
        self.generic_visit(node)

    # ------------------------------------------------------------ secrets

    def _check_secret(self, target: ast.AST, value: ast.AST, node: ast.AST) -> None:
        name = (
            target.id
            if isinstance(target, ast.Name)
            else (target.attr if isinstance(target, ast.Attribute) else "")
        )
        if name:
            self._check_secret_name(name, value, node)

    def _check_secret_name(self, name: str, value: ast.AST, node: ast.AST) -> None:
        if not SECRET_NAME.search(name) or SECRET_NAME_EXCLUDE.search(name):
            return
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            return
        v = value.value
        if len(v) < 8 or any(c.isspace() for c in v) or PLACEHOLDER.search(v):
            return
        if v.startswith(("http://", "https://", "/", "./", "../", "sqlite:", "postgres")):
            return
        if len(set(v)) < 5:
            return
        classes = sum(
            bool(re.search(p, v)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
        )
        if classes < 2 and len(v) < 20:
            return
        self.add("RB-SECRET", node, f"`{name}` is assigned a hardcoded credential-like literal.")

    # ------------------------------------------------------------ calls

    def visit_Call(self, node: ast.Call) -> None:  # noqa: C901 - rule dispatch
        name = dotted(node.func)
        short = name.rsplit(".", 1)[-1]
        first = node.args[0] if node.args else None

        # keyword secrets: connect(password="...")
        for k in node.keywords:
            if k.arg:
                self._check_secret_name(k.arg, k.value, node)

        # --- SQL injection
        if short in SQL_SINKS and first is not None:
            target = self.resolve(first)
            # execute(text(f"...")) is reported once, on the inner text() call.
            if self.is_dynamic_str(first) and SQL_KEYWORDS.search(const_text(target)):
                self.add(
                    "RB-SQLI",
                    node,
                    f"`{name}()` receives SQL built with string interpolation; use bound "
                    "parameters instead.",
                    confidence="high",
                )

        # --- command injection
        if (
            name.startswith("subprocess.")
            and short in {"run", "call", "check_call", "check_output", "Popen"}
            and _is_true(_kw(node, "shell"))
        ):
            dyn = first is not None and (
                self.is_dynamic_str(first) or isinstance(self.resolve(first), ast.Name)
            )
            self.add(
                "RB-CMDI",
                node,
                f"`{name}(..., shell=True)` runs through the shell.",
                severity=Severity.high if dyn else Severity.medium,
                confidence="high" if dyn else "medium",
            )
        elif (
            name
            in {
                "os.system",
                "os.popen",
                "subprocess.getoutput",
                "subprocess.getstatusoutput",
                "asyncio.create_subprocess_shell",
                "commands.getoutput",
            }
            and first is not None
        ):
            dyn = not isinstance(first, ast.Constant)
            self.add(
                "RB-CMDI",
                node,
                f"`{name}()` executes a shell command.",
                severity=Severity.high if dyn else Severity.low,
            )

        # --- eval / exec
        if name in {"eval", "exec"} and first is not None and not isinstance(first, ast.Constant):
            self.add("RB-EVAL", node, f"`{name}()` on non-constant input.", confidence="high")

        # --- deserialization
        if name in {
            "pickle.loads",
            "pickle.load",
            "cPickle.loads",
            "cPickle.load",
            "dill.loads",
            "dill.load",
            "marshal.loads",
            "marshal.load",
            "jsonpickle.decode",
            "shelve.open",
        }:
            self.add("RB-PICKLE", node, f"`{name}()` can execute code from untrusted data.")
        if name in {"yaml.load", "yaml.load_all"}:
            loader = _kw(node, "Loader") or (node.args[1] if len(node.args) > 1 else None)
            if loader is None or not dotted(loader).endswith("SafeLoader"):
                self.add(
                    "RB-YAML-LOAD",
                    node,
                    "`yaml.load` without `SafeLoader` can construct arbitrary Python objects; "
                    "use `yaml.safe_load`.",
                    confidence="high",
                )
        if name in {"yaml.unsafe_load", "yaml.unsafe_load_all"}:
            self.add("RB-YAML-LOAD", node, f"`{name}` constructs arbitrary Python objects.")

        # --- JWT
        is_jwt = name.endswith("jwt.decode") or name in {"jwt.decode", "jose.jwt.decode"}
        if is_jwt:
            opts = _kw(node, "options")
            bad = _is_false(_kw(node, "verify"))
            if isinstance(opts, ast.Dict):
                for k, v in zip(opts.keys, opts.values, strict=False):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "verify_signature"
                        and _is_false(v)
                    ):
                        bad = True
            algs = _kw(node, "algorithms")
            if isinstance(algs, ast.List) and any(
                isinstance(e, ast.Constant) and str(e.value).lower() == "none" for e in algs.elts
            ):
                bad = True
            if bad:
                self.add("RB-JWT-NOVERIFY", node, "JWT signature verification is disabled.")

        # --- TLS verification
        if not is_jwt and _is_false(_kw(node, "verify")):
            self.add("RB-TLS-VERIFY", node, f"`{name}(verify=False)` disables TLS verification.")
        if name == "ssl._create_unverified_context":
            self.add("RB-TLS-VERIFY", node, "Unverified SSL context.")

        # --- debug
        if _is_true(_kw(node, "debug")):
            self.add("RB-DEBUG", node, f"`{name}(debug=True)` leaks stack traces and internals.")

        # --- open redirect
        if short in {"RedirectResponse", "redirect"}:
            target = _kw(node, "url") or first
            if target is not None and self.request_derived(target):
                self.add(
                    "RB-OPEN-REDIRECT",
                    node,
                    "Redirect target comes from the request; restrict it to same-site "
                    "relative paths.",
                    confidence="high",
                )

        # --- XSS via HTMLResponse / Markup
        if short == "HTMLResponse":
            content = _kw(node, "content") or first
            if content is not None and self._unescaped_html(content):
                self.add(
                    "RB-XSS-HTMLRESPONSE",
                    node,
                    "HTML is built by interpolating values without escaping.",
                    confidence="high",
                )
        if short == "Markup" and first is not None and self.is_dynamic_str(first):
            self.add("RB-XSS-MARKUP", node, "`Markup()` marks interpolated content as safe HTML.")

        # --- CORS
        if (
            short == "add_middleware"
            and first is not None
            and dotted(first).endswith("CORSMiddleware")
        ):
            self._check_cors(node)
        if short == "CORSMiddleware":
            self._check_cors(node)

        # --- Jinja autoescape
        if short in {"Environment", "Jinja2Templates"} and (
            name.startswith(("jinja2.", "Environment", "Jinja2Templates", "templating."))
            or short == "Jinja2Templates"
        ):
            ae = _kw(node, "autoescape")
            if _is_false(ae):
                self.add("RB-JINJA-AUTOESCAPE-OFF", node, "Jinja autoescaping is turned off.")
            elif ae is None and short == "Environment":
                self.add(
                    "RB-JINJA-AUTOESCAPE-OFF",
                    node,
                    "`jinja2.Environment()` defaults to autoescape=False; pass "
                    "`autoescape=select_autoescape()`.",
                    severity=Severity.medium,
                )

        # --- weak password hashing
        if name in {"hashlib.md5", "hashlib.sha1", "md5", "sha1"} or (
            name == "hashlib.new"
            and first is not None
            and isinstance(first, ast.Constant)
            and str(first.value).lower() in {"md5", "sha1"}
        ):
            ctx_text = self._stmt_text() + (self.func_stack[-1].name if self.func_stack else "")
            if PASSWORD_CONTEXT.search(ctx_text):
                self.add(
                    "RB-WEAK-HASH",
                    node,
                    "Passwords hashed with a fast digest; use argon2/bcrypt/scrypt.",
                )

        # --- weak randomness for secrets
        if name.startswith("random.") and short in {
            "random",
            "randint",
            "choice",
            "choices",
            "getrandbits",
            "randrange",
            "sample",
            "shuffle",
        }:
            ctx_text = (
                self._stmt_text() + " " + (self.func_stack[-1].name if self.func_stack else "")
            )
            if SECRETY_CONTEXT.search(ctx_text):
                self.add(
                    "RB-WEAK-RANDOM",
                    node,
                    "`random` is predictable; use the `secrets` module for tokens.",
                )

        self.generic_visit(node)

    def _unescaped_html(self, content: ast.AST) -> bool:
        node = self.resolve(content)
        if isinstance(node, ast.JoinedStr):
            return any(
                isinstance(v, ast.FormattedValue)
                and not _is_const_name(v.value)
                and not _escaped(v.value)
                for v in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            leaves = _concat_leaves(node)
            return any(
                not isinstance(leaf, ast.Constant) and not _is_const_name(leaf)
                and not _escaped(leaf)
                and not (isinstance(leaf, ast.JoinedStr) and not self._unescaped_html(leaf))
                for leaf in leaves
            ) and any(const_text(leaf) for leaf in leaves)
        if isinstance(node, ast.BinOp | ast.Call) and self.is_dynamic_str(node, depth=1):
            return not _escaped(node)
        return False

    def _check_cors(self, node: ast.Call) -> None:
        origins = _kw(node, "allow_origins")
        regex = _kw(node, "allow_origin_regex")
        wildcard = (
            isinstance(origins, ast.List | ast.Tuple)
            and any(isinstance(e, ast.Constant) and e.value == "*" for e in origins.elts)
        ) or (isinstance(regex, ast.Constant) and str(regex.value) in {".*", "^.*$", ".+"})
        if wildcard and _is_true(_kw(node, "allow_credentials")):
            self.add(
                "RB-CORS",
                node,
                "Starlette reflects any Origin when allow_origins=['*'] and "
                "allow_credentials=True, so any site can make credentialed requests.",
            )

    # ------------------------------------------------------------ authz heuristic

    def _check_authz(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if route_info(func) is None:
            return
        args = func.args
        all_args = args.posonlyargs + args.args + args.kwonlyargs
        defaults = dict(
            zip(
                [a.arg for a in (args.posonlyargs + args.args)][-len(args.defaults) :]
                if args.defaults
                else [],
                args.defaults,
                strict=False,
            )
        )
        defaults.update(
            {
                a.arg: d
                for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=False)
                if d is not None
            }
        )
        id_params = [a.arg for a in all_args if a.arg == "id" or a.arg.endswith("_id")]
        if not id_params:
            return
        seg_all = [ast.get_source_segment(self.source, d) or "" for d in defaults.values()]
        seg_all += [
            ast.get_source_segment(self.source, a.annotation) or ""
            for a in all_args
            if a.annotation
        ]
        for dec in func.decorator_list:
            seg_all.append(ast.get_source_segment(self.source, dec) or "")
        if any("Depends" in s and AUTH_DEP_WORDS.search(s) for s in seg_all):
            return
        if any("Security(" in s for s in seg_all):
            return
        uses_db = False
        for n in ast.walk(func):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr in DB_CALLS or (n.func.attr == "get" and len(n.args) == 2):
                    uses_db = True
                    break
        if uses_db:
            method, path = route_info(func) or ("GET", "")
            self.add(
                "RB-AUTHZ",
                func,
                f"{method} {path} looks up `{', '.join(id_params)}` without an auth "
                "dependency (possible IDOR). Heuristic: verify router-level auth.",
                confidence="low",
            )


TEMPLATE_SUFFIXES = (".html", ".htm", ".jinja", ".jinja2", ".j2", ".tmpl")
SAFE_FILTER = re.compile(r"\{\{(?P<expr>[^}]*?)\|\s*safe\b[^}]*\}\}")
AUTOESCAPE_OFF = re.compile(r"\{%-?\s*autoescape\s+(false|False)\s*-?%\}")


def scan_template(text: str, rel: str) -> list[Finding]:
    out: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in SAFE_FILTER.finditer(line):
            expr = m.group("expr").strip()
            user_like = bool(USER_CONTENT_WORDS.search(expr))
            rule = RULES["RB-JINJA-SAFE"]
            out.append(
                Finding(
                    tool="redblue-sast",
                    rule_id=rule.id,
                    title=rule.title,
                    description=f"`{{{{ {expr}|safe }}}}` renders without escaping"
                    + (" and looks like user-supplied content." if user_like else "."),
                    severity=Severity.high if user_like else rule.severity,
                    confidence="high" if user_like else "medium",
                    cwe=rule.cwe,
                    file=rel,
                    line=lineno,
                    evidence=line.strip()[:500],
                    code=m.group(0),
                )
            )
        if AUTOESCAPE_OFF.search(line):
            rule = RULES["RB-JINJA-AUTOESCAPE-OFF"]
            out.append(
                Finding(
                    tool="redblue-sast",
                    rule_id=rule.id,
                    title=rule.title,
                    description="`{% autoescape false %}` block disables escaping.",
                    severity=rule.severity,
                    cwe=rule.cwe,
                    file=rel,
                    line=lineno,
                    evidence=line.strip()[:500],
                    code=line.strip(),
                )
            )
    return out


def scan_python_source(source: str, rel: str) -> list[Finding]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    v = _Visitor(source, rel)
    v.visit(tree)
    return v.findings


class BuiltinSast:
    name = "builtin_sast"
    kind = "sast"

    def run(self, ctx: ScanContext) -> list[Finding]:
        out: list[Finding] = []
        for path in iter_files(ctx, (".py",) + TEMPLATE_SUFFIXES):
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = ctx.rel(path)
            if path.suffix == ".py":
                out.extend(scan_python_source(text, rel))
            else:
                out.extend(scan_template(text, rel))
        return out


def scan_path(path: str | Path) -> list[Finding]:
    """Convenience for tests/evals: scan one file or directory with default config."""
    from redblue.gate.config import GateConfig

    p = Path(path)
    root = p if p.is_dir() else p.parent
    cfg = GateConfig(paths=["." if p.is_dir() else p.name])
    return BuiltinSast().run(ScanContext(repo_path=root, config=cfg))
