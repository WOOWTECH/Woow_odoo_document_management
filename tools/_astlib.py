# Copyright 2026 WoowTech
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Shared helpers for the static tools in tools/ (IF-STATIC).

Standard library only: the tools run as ``python3 -I tools/check_<name>.py`` (isolated mode,
no user site, so no PyYAML). ``load_yaml`` is a small loader for the YAML subset used by the
repo's own config files.

Command line contract of every tool::

    python3 -I tools/check_<name>.py [--g1] [--root <repo>]

Output: one ``<path>:<line> <RULE> <message>`` line per violation.
Exit codes: 0 pass, 1 violations, 2 the tool itself could not run.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field

ADDON = 'woow_document_management'
FIXTURES_DIR = 'tools/tests/fixtures'
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv'}


class ToolError(Exception):
    """The tool cannot run (exit code 2)."""


@dataclass(order=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str = field(compare=False)

    def __str__(self):
        return '%s:%d %s %s' % (self.path, self.line, self.rule, self.message)


# --------------------------------------------------------------------------- command line

def make_parser(description, extra=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--g1', action='store_true',
                        help='release gate mode: exact counts, no WOOW-STUB markers')
    parser.add_argument('--root', default=None, help='repository root (default: parent of tools/)')
    if extra:
        extra(parser)
    return parser


def default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_tool(check, description, argv=None, extra=None):
    """Parse arguments, run ``check(root, args) -> list[Violation]``, print and exit."""
    parser = make_parser(description, extra)
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root or default_root())
    try:
        if not os.path.isdir(root):
            raise ToolError('root %s is not a directory' % root)
        violations = check(root, args)
    except ToolError as exc:
        print('tool error: %s' % exc, file=sys.stderr)
        return 2
    except (OSError, SyntaxError, ValueError) as exc:
        print('tool error: %s: %s' % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    if violations is None:      # the tool printed its own output (e.g. --emit-entry-points)
        return 0
    for v in sorted(violations):
        print(v)
    return 1 if violations else 0


# --------------------------------------------------------------------------- files

def rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, '/')


def is_fixture(relpath):
    return relpath == FIXTURES_DIR or relpath.startswith(FIXTURES_DIR + '/')


def is_test_path(relpath):
    parts = relpath.split('/')
    return 'tests' in parts[:-1]


def iter_files(root, exts=None, include_tests=True, include_fixtures=False):
    """Yield (relpath, abspath) under root, sorted, skipping VCS and cache dirs."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            path = os.path.join(dirpath, name)
            r = rel(root, path)
            if not include_fixtures and is_fixture(r):
                continue
            if not include_tests and is_test_path(r):
                continue
            if exts and not any(name.endswith(e) for e in exts):
                continue
            out.append((r, path))
    out.sort()
    return out


def read_text(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def try_read_text(path):
    try:
        return read_text(path)
    except (UnicodeDecodeError, OSError):
        return None


# --------------------------------------------------------------------------- python sources

@dataclass
class PySource:
    relpath: str
    text: str
    tree: ast.AST
    comments: dict      # line -> comment text (without '#')

    def comment(self, line):
        return self.comments.get(line, '')

    def segment_lines(self, node):
        end = getattr(node, 'end_lineno', None) or node.lineno
        return range(node.lineno, end + 1)

    def comment_for(self, node):
        """All comments on the lines spanned by ``node`` (for multi-line calls)."""
        return ' '.join(self.comments.get(n, '') for n in self.segment_lines(node))


def parse_py(root, relpath):
    text = read_text(os.path.join(root, relpath))
    tree = ast.parse(text, filename=relpath)
    comments = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.string.lstrip('#').strip()
    except (tokenize.TokenError, IndentationError):
        pass
    attach_parents(tree)
    return PySource(relpath, text, tree, comments)


def attach_parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node


def parent(node):
    return getattr(node, '_parent', None)


def enclosing(node, types):
    cur = parent(node)
    while cur is not None and not isinstance(cur, types):
        cur = parent(cur)
    return cur


def enclosing_function(node):
    return enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))


def enclosing_class(node):
    return enclosing(node, ast.ClassDef)


def class_assign(cls, name):
    """Value node of ``name = ...`` directly in the class body, or None."""
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id == name and stmt.value is not None:
                return stmt.value
    return None


def const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def class_model_names(cls):
    """Model names of a class: the ``_name`` literal, else the string ``_inherit`` values."""
    name = const_str(class_assign(cls, '_name'))
    if name:
        return [name]
    inherit = class_assign(cls, '_inherit')
    if inherit is None:
        return []
    single = const_str(inherit)
    if single:
        return [single]
    if isinstance(inherit, (ast.List, ast.Tuple)):
        return [s for s in (const_str(e) for e in inherit.elts) if s]
    return []


def class_model(cls):
    """Model of a class for the sudo allowlist (ER-44).

    The ``_name`` literal first; without it, the single string ``_inherit`` (a string, or a list
    with exactly one string). Anything else -> None (counted as a violation by callers).
    """
    name = const_str(class_assign(cls, '_name'))
    if name:
        return name
    inherit = class_assign(cls, '_inherit')
    single = const_str(inherit) if inherit is not None else None
    if single:
        return single
    if isinstance(inherit, (ast.List, ast.Tuple)) and len(inherit.elts) == 1:
        return const_str(inherit.elts[0])
    return None


def class_kind(cls):
    """'Model', 'AbstractModel', 'TransientModel' or None from the class bases."""
    for base in cls.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, 'id', None)
        if name in ('Model', 'AbstractModel', 'TransientModel'):
            return name
    return None


def call_name(node):
    """Last name of a call target: ``a.b.c(...)`` -> 'c', ``f(...)`` -> 'f'."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def dotted(node):
    """Source-like text of an expression (best effort, for messages and prefix checks)."""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover
        return '<expr>'


def kw(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def is_const(node, value):
    return isinstance(node, ast.Constant) and node.value is value


def first_statement(func):
    """First statement of a function body, skipping the docstring."""
    body = list(func.body)
    if body and isinstance(body[0], ast.Expr) and const_str(body[0].value) is not None:
        body = body[1:]
    return body[0] if body else None


def body_without_docstring(func):
    body = list(func.body)
    if body and isinstance(body[0], ast.Expr) and const_str(body[0].value) is not None:
        body = body[1:]
    return body


def is_stub_raise(stmt, src):
    """``raise NotImplementedError  # WOOW-STUB``."""
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return False
    exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
    return (isinstance(exc, ast.Name) and exc.id == 'NotImplementedError'
            and 'WOOW-STUB' in src.comment(stmt.lineno))


# --------------------------------------------------------------------------- git

def git_toplevel(root):
    try:
        out = subprocess.run(['git', '-C', root, 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return os.path.realpath(out.stdout.strip())


def is_git_root(root):
    top = git_toplevel(root)
    return top is not None and top == os.path.realpath(root)


def git_index_entries(root):
    """[(mode, path)] from ``git ls-files -s`` (only when root is the work tree top)."""
    out = subprocess.run(['git', '-C', root, 'ls-files', '-s', '-z'], capture_output=True,
                         check=True, timeout=60)
    entries = []
    for chunk in out.stdout.decode('utf-8', 'replace').split('\0'):
        if not chunk:
            continue
        meta, _, path = chunk.partition('\t')
        entries.append((meta.split(' ')[0], path))
    return entries


# --------------------------------------------------------------------------- YAML subset

class YamlError(ValueError):
    pass


def _strip_comment(line):
    """Remove a trailing ' # comment' that is outside quotes."""
    out, quote, i = [], None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if quote == "'" and i + 1 < len(line) and line[i + 1] == "'":
                    out.append("'")
                    i += 1
                else:
                    quote = None
        elif ch in ('"', "'") and (not out or out[-1] in ' [{,:-'):
            quote = ch
            out.append(ch)
        elif ch == '#' and (not out or out[-1] in ' \t'):
            break
        else:
            out.append(ch)
        i += 1
    return ''.join(out).rstrip()


_INT_RE = re.compile(r'^-?\d+$')


def _scalar(text):
    text = text.strip()
    if text == '' or text in ('~', 'null', 'Null', 'NULL'):
        return None
    if text in ('true', 'True', 'TRUE'):
        return True
    if text in ('false', 'False', 'FALSE'):
        return False
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            raise YamlError('unterminated string: %s' % text)
        return json.loads(text)
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise YamlError('unterminated string: %s' % text)
        return text[1:-1].replace("''", "'")
    if _INT_RE.match(text):
        return int(text)
    return text


class _Flow:
    """Parser for flow collections: [a, b], {k: v}."""

    def __init__(self, text):
        self.s = text
        self.i = 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in ' \t\n':
            self.i += 1

    def parse(self):
        self.ws()
        value = self.value()
        self.ws()
        if self.i != len(self.s):
            raise YamlError('trailing text in flow value: %s' % self.s)
        return value

    def value(self):
        self.ws()
        ch = self.s[self.i:self.i + 1]
        if ch == '[':
            self.i += 1
            items = []
            while True:
                self.ws()
                if self.s[self.i:self.i + 1] == ']':
                    self.i += 1
                    return items
                items.append(self.value())
                self.ws()
                nxt = self.s[self.i:self.i + 1]
                if nxt == ',':
                    self.i += 1
                elif nxt != ']':
                    raise YamlError('bad flow sequence: %s' % self.s)
        if ch == '{':
            self.i += 1
            mapping = {}
            while True:
                self.ws()
                if self.s[self.i:self.i + 1] == '}':
                    self.i += 1
                    return mapping
                key = self.scalar(stop=':')
                self.ws()
                if self.s[self.i:self.i + 1] != ':':
                    raise YamlError('missing ":" in flow mapping: %s' % self.s)
                self.i += 1
                mapping[key] = self.value()
                self.ws()
                nxt = self.s[self.i:self.i + 1]
                if nxt == ',':
                    self.i += 1
                elif nxt != '}':
                    raise YamlError('bad flow mapping: %s' % self.s)
        return self.scalar(stop=',]}')

    def scalar(self, stop):
        self.ws()
        ch = self.s[self.i:self.i + 1]
        if ch in ('"', "'"):
            j = self.i + 1
            while j < len(self.s):
                if self.s[j] == '\\' and ch == '"':
                    j += 2
                    continue
                if self.s[j] == ch:
                    if ch == "'" and self.s[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    break
                j += 1
            token = self.s[self.i:j + 1]
            self.i = j + 1
            return _scalar(token)
        j = self.i
        while j < len(self.s):
            c = self.s[j]
            if c in stop and not (c == ':' and self.s[j + 1:j + 2] not in (' ', '')):
                break
            j += 1
        token = self.s[self.i:j]
        self.i = j
        return _scalar(token)


def _split_key(text):
    """'key: value' -> (key, value) with quotes and ': ' inside quotes respected; else None."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'") and i == 0:
            quote = ch
            continue
        if ch == ':' and (i + 1 == len(text) or text[i + 1] == ' '):
            key = _scalar(text[:i])
            return key, text[i + 1:].strip()
        if ch in '[{' and i == 0:
            return None
    return None


def _value(text):
    text = text.strip()
    if text.startswith('[') or text.startswith('{'):
        return _Flow(text).parse()
    return _scalar(text)


def load_yaml(text):
    """Load the YAML subset used by this repo.

    Supported: block mappings and sequences, flow collections, plain / single / double quoted
    scalars, ints, booleans, null, comments, folded (``>``, ``>-``) and literal (``|``, ``|-``)
    block scalars. Not supported (YamlError): anchors, tags, multi-document streams.
    """
    raw_lines = text.replace('\t', '    ').split('\n')
    lines = []          # (indent, content, raw_index)
    block_scalars = {}  # raw_index -> (style, raw_index_list)
    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i]
        stripped = _strip_comment(raw)
        if not stripped.strip():
            i += 1
            continue
        if stripped.strip() == '---' and not lines:
            i += 1
            continue
        if stripped.lstrip().startswith(('&', '*', '!')):
            raise YamlError('unsupported YAML feature on line %d' % (i + 1))
        indent = len(stripped) - len(stripped.lstrip(' '))
        content = stripped.strip()
        lines.append((indent, content, i))
        m = re.search(r'(?:^|:\s|^-\s)([>|])(-?)$', content)
        if m:
            # collect the block scalar body: following lines more indented than this one
            body, j = [], i + 1
            while j < len(raw_lines):
                nxt = raw_lines[j]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip(' '))) <= indent:
                    break
                body.append(nxt)
                j += 1
            block_scalars[i] = (m.group(1), m.group(2), body)
            i = j
            continue
        i += 1

    pos = [0]

    def block_scalar_value(raw_index):
        style, chomp, body = block_scalars[raw_index]
        non_empty = [b for b in body if b.strip()]
        if not non_empty:
            return ''
        ind = min(len(b) - len(b.lstrip(' ')) for b in non_empty)
        rows = [b[ind:] if b.strip() else '' for b in body]
        while rows and rows[-1] == '':
            rows.pop()
        if style == '|':
            value = '\n'.join(rows)
        else:
            parts, para = [], []
            for row in rows:
                if row == '':
                    parts.append(' '.join(para))
                    para = []
                else:
                    para.append(row.strip())
            parts.append(' '.join(para))
            value = '\n'.join(parts)
        return value if chomp == '-' else value + '\n'

    def item_value(rest, raw_index, child_indent):
        if raw_index in block_scalars:
            return block_scalar_value(raw_index)
        if rest == '':
            if pos[0] < len(lines) and lines[pos[0]][0] >= child_indent:
                return parse_block(lines[pos[0]][0])
            if pos[0] < len(lines) and lines[pos[0]][1].startswith('- ') \
                    and lines[pos[0]][0] == child_indent - 1:
                return parse_block(lines[pos[0]][0])
            return None
        return _value(rest)

    def parse_block(indent):
        first = lines[pos[0]][1]
        if first == '-' or first.startswith('- '):
            return parse_seq(indent)
        return parse_map(indent)

    def parse_seq(indent):
        items = []
        while pos[0] < len(lines):
            ind, content, raw_index = lines[pos[0]]
            if ind < indent or not (content == '-' or content.startswith('- ')):
                break
            if ind > indent:
                raise YamlError('bad indentation on line %d' % (raw_index + 1))
            pos[0] += 1
            rest = content[1:].strip()
            kv = _split_key(rest) if rest and rest[0] not in '[{"\'' else None
            if kv is not None and not rest.startswith(('>', '|')):
                # "- key: value" starts an inline mapping
                inner = ind + 2
                key, val = kv
                mapping = {key: item_value(val, raw_index, inner + 1)}
                while pos[0] < len(lines) and lines[pos[0]][0] == inner \
                        and not lines[pos[0]][1].startswith('- '):
                    mapping.update(parse_map(inner, single=True))
                items.append(mapping)
            else:
                items.append(item_value(rest, raw_index, ind + 1))
        return items

    def parse_map(indent, single=False):
        mapping = {}
        while pos[0] < len(lines):
            ind, content, raw_index = lines[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError('bad indentation on line %d' % (raw_index + 1))
            if content.startswith('- '):
                break
            kv = _split_key(content)
            if kv is None:
                raise YamlError('expected "key: value" on line %d' % (raw_index + 1))
            pos[0] += 1
            key, rest = kv
            if key in mapping:
                raise YamlError('duplicate key %r on line %d' % (key, raw_index + 1))
            mapping[key] = item_value(rest, raw_index, ind + 1)
            if single:
                break
        return mapping

    if not lines:
        return None
    result = parse_block(lines[0][0])
    if pos[0] != len(lines):
        raise YamlError('unparsed content on line %d' % (lines[pos[0]][2] + 1))
    return result


def load_yaml_file(path):
    return load_yaml(read_text(path))
